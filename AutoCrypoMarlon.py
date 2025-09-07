# -*- coding: utf-8 -*-
import telegram
from telegram.ext import Application, CommandHandler
import logging
import time
import os
from dotenv import load_dotenv
import asyncio
from base64 import b64decode
import pandas as pd
import pandas_ta as ta
import httpx
from datetime import datetime, timezone

# --- Libs da Solana ---
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from spl.token.instructions import get_associated_token_address

from flask import Flask
from threading import Thread

# --- CÓDIGO DO SERVIDOR WEB ---
app = Flask('')
@app.route('/')
def home():
    return "Bot is alive!"
def run_server():
  app.run(host='0.0.0.0',port=8080)
def keep_alive():
    t = Thread(target=run_server)
    t.start()
# --- FIM DO CÓDIGO DO SERVIDOR ---

load_dotenv()

# --- Configurações Iniciais ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
PRIVATE_KEY_B58 = os.getenv("PRIVATE_KEY_BASE58")
RPC_URL = os.getenv("RPC_URL")

if not all([TELEGRAM_TOKEN, CHAT_ID, PRIVATE_KEY_B58, RPC_URL]):
    print("Erro: Verifique se todas as variáveis de ambiente estão definidas.")
    exit()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)

try:
    solana_client = Client(RPC_URL)
    payer = Keypair.from_base58_string(PRIVATE_KEY_B58)
    logger.info(f"Carteira carregada com sucesso. Endereço público: {payer.pubkey()}")
except Exception as e:
    logger.error(f"Erro ao carregar a carteira Solana: {e}")
    exit()

# --- Variáveis Globais de Estado ---
bot_running = False
in_position = False
entry_price = 0.0
periodic_task = None
application = None
sell_fail_count = 0
buy_fail_count = 0

automation_state = {
    "current_target_pair_address": None,
    "current_target_symbol": None,
    "current_target_pair_details": None,
    "last_scan_timestamp": 0,
    "position_opened_timestamp": 0,
    "target_selected_timestamp": 0,
    "penalty_box": {},
    "discovered_pairs": {},
    "last_price_change_pct": None, 
    "last_price_change_timestamp": 0,
    "checking_volatility": False,
    "volatility_check_start_time": 0,
    "volatility_check_passed": False
}

parameters = {
    "timeframe": "1m",
    "amount": None,
    "stop_loss_percent": None,
    "take_profit_percent": None,
    "priority_fee": 2000000
}

# --- Funções de Execução de Ordem ---
async def execute_swap(input_mint_str, output_mint_str, amount, input_decimals, slippage_bps):
    logger.info(f"Iniciando swap de {amount} do token {input_mint_str} para {output_mint_str} com slippage de {slippage_bps} BPS e limite computacional dinâmico.")
    amount_wei = int(amount * (10**input_decimals))
    
    async with httpx.AsyncClient() as client:
        try:
            quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint_str}&outputMint={output_mint_str}&amount={amount_wei}&slippageBps={slippage_bps}&maxAccounts=64"
            quote_res = await client.get(quote_url, timeout=60.0)
            quote_res.raise_for_status()
            quote_response = quote_res.json()

            priority_fee = parameters.get("priority_fee")
            
            swap_payload = { 
                "userPublicKey": str(payer.pubkey()), 
                "quoteResponse": quote_response, 
                "wrapAndUnwrapSol": True, 
                "dynamicComputeUnitLimit": True,
                "prioritizationFee": priority_fee
            }
            
            swap_url = "https://quote-api.jup.ag/v6/swap"
            swap_res = await client.post(swap_url, json=swap_payload, timeout=60.0)
            swap_res.raise_for_status()
            swap_response = swap_res.json()
            swap_tx_b64 = swap_response.get('swapTransaction')
            if not swap_tx_b64:
                logger.error(f"Erro na API da Jupiter: {swap_response}"); return None
            
            raw_tx_bytes = b64decode(swap_tx_b64)
            swap_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
            signature = payer.sign_message(to_bytes_versioned(swap_tx.message))
            signed_tx = VersionedTransaction.populate(swap_tx.message, [signature])

            tx_opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
            tx_signature = solana_client.send_raw_transaction(bytes(signed_tx), opts=tx_opts).value
            
            logger.info(f"Transação enviada: {tx_signature}")
            await asyncio.sleep(12)
            solana_client.confirm_transaction(tx_signature, commitment="confirmed")
            logger.info(f"Transação confirmada: https://solscan.io/tx/{tx_signature}")
            
            # --- LÓGICA ORIGINAL RESTAURADA ---
            return str(tx_signature)

        except Exception as e:
            logger.error(f"Falha na transação: {e}"); await send_telegram_message(f"⚠️ Falha na transação: {e}"); return None


async def execute_buy_order(amount, price, pair_details, manual=False, reason="Sinal da Estratégia"):
    global in_position, entry_price, sell_fail_count, buy_fail_count
    if in_position: return

    if not manual:
        logger.info(f"Verificação final de cotação para {pair_details['base_symbol']} antes da compra...")
        if not await is_pair_quotable_on_jupiter(pair_details):
            logger.error(f"FALHA NA COMPRA: Par {pair_details['base_symbol']} deixou de ser negociável na Jupiter. Penalizando e procurando novo alvo.")
            await send_telegram_message(f"❌ Compra para **{pair_details['base_symbol']}** abortada. Moeda não mais negociável na Jupiter.")
            automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
            automation_state["current_target_pair_address"] = None
            return

    slippage_bps = await calculate_dynamic_slippage(pair_details['pair_address'])
    logger.info(f"EXECUTANDO ORDEM DE COMPRA de {amount} SOL para {pair_details['base_symbol']} ao preço de {price}")
    
    tx_sig = await execute_swap(pair_details['quote_address'], pair_details['base_address'], amount, 9, slippage_bps)

    if tx_sig:
        in_position = True
        entry_price = price
        automation_state["position_opened_timestamp"] = time.time()
        sell_fail_count = 0
        buy_fail_count = 0
        log_message = (f"✅ COMPRA REALIZADA: {amount} SOL para {pair_details['base_symbol']}\n"
                       f"Motivo: {reason}\n"
                       f"Entrada: {price:.10f} | Alvo: {price * (1 + parameters['take_profit_percent']/100):.10f} | "
                       f"Stop: {price * (1 - parameters['stop_loss_percent']/100):.10f}\n"
                       f"Slippage Usado: {slippage_bps/100:.2f}%\n"
                       f"Taxa de Prioridade: {parameters.get('priority_fee')} micro-lamports\n"
                       f"https://solscan.io/tx/{tx_sig}")
        logger.info(log_message)
        await send_telegram_message(log_message)
    else:
        buy_fail_count += 1
        logger.error(f"FALHA NA EXECUÇÃO da compra para {pair_details['base_symbol']}. Tentativa {buy_fail_count}/10. O bot tentará novamente.")
        if buy_fail_count >= 10:
            logger.error(f"FALHA NA EXECUÇÃO da compra para {pair_details['base_symbol']}. Limite de {buy_fail_count} falhas atingido. Penalizando e procurando novo alvo.")
            await send_telegram_message(f"❌ FALHA NA EXECUÇÃO da compra para **{pair_details['base_symbol']}**. Limite de 10 falhas atingido. A moeda será penalizada.")
            if automation_state.get("current_target_pair_address"):
                automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                automation_state["current_target_pair_address"] = None
            buy_fail_count = 0


async def execute_sell_order(reason=""):
    global in_position, entry_price, sell_fail_count, buy_fail_count
    if not in_position: return
    
    pair_details = automation_state.get('current_target_pair_details', {})
    symbol = pair_details.get('base_symbol', 'TOKEN')
    logger.info(f"EXECUTANDO ORDEM DE VENDA de {symbol}. Motivo: {reason}")
    try:
        token_mint_pubkey = Pubkey.from_string(pair_details['base_address'])
        ata_address = get_associated_token_address(payer.pubkey(), token_mint_pubkey)
        
        balance_response = solana_client.get_token_account_balance(ata_address)

        if hasattr(balance_response, 'value'):
            token_balance_data = balance_response.value
        else:
            logger.error(f"Erro ao obter saldo do token {symbol}: Resposta RPC inválida.")
            sell_fail_count += 1
            if sell_fail_count >= 100:
                logger.error("ATINGIDO LIMITE DE FALHAS DE VENDA. RESETANDO POSIÇÃO.")
                await send_telegram_message(f"⚠️ Limite de 100 falhas de venda para **{symbol}** atingido. Posição abandonada para evitar loop.")
                in_position = False; entry_price = 0.0; automation_state["position_opened_timestamp"] = 0
                
                if automation_state.get('current_target_pair_address'):
                    automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                    automation_state["current_target_pair_address"] = None
                    await send_telegram_message(f"⚠️ **{symbol}** foi penalizada por 10 ciclos após falhas de venda.")
                
                sell_fail_count = 0
            await send_telegram_message(f"⚠️ Erro ao obter saldo do token {symbol}. A venda falhou. Tentativa {sell_fail_count}/100. O bot permanecerá em posição.")
            return

        amount_to_sell = token_balance_data.ui_amount
        if amount_to_sell is None or amount_to_sell == 0:
            logger.warning("Tentativa de venda com saldo zero, resetando posição.")
            in_position = False; entry_price = 0.0; automation_state["position_opened_timestamp"] = 0; automation_state["current_target_pair_address"] = None
            sell_fail_count = 0
            return

        slippage_bps = await calculate_dynamic_slippage(pair_details['pair_address'])
        tx_sig = await execute_swap(pair_details['base_address'], pair_details['quote_address'], amount_to_sell, token_balance_data.decimals, slippage_bps)
        
        if tx_sig:
            log_message = (f"🛑 VENDA REALIZADA: {symbol}\n"
                           f"Motivo: {reason}\n"
                           f"Slippage Usado: {slippage_bps/100:.2f}%\n"
                           f"Taxa de Prioridade: {parameters.get('priority_fee')} micro-lamports\n"
                           f"https://solscan.io/tx/{tx_sig}")
            logger.info(log_message)
            await send_telegram_message(log_message)
            in_position = False; entry_price = 0.0; automation_state["position_opened_timestamp"] = 0
            
            if "Stop Loss" in reason or "Timeout" in reason:
                if automation_state.get('current_target_pair_address'):
                    automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                    automation_state["current_target_pair_address"] = None
                    await send_telegram_message(f"⚠️ **{symbol}** foi penalizada por 10 ciclos após a venda por stop/timeout.")
            else:
                automation_state["current_target_pair_address"] = None
            
            sell_fail_count = 0
            buy_fail_count = 0
        else:
            logger.error(f"FALHA NA VENDA do token {symbol}. O bot permanecerá em posição e tentará vender novamente.")
            sell_fail_count += 1
            if sell_fail_count >= 100:
                logger.error("ATINGIDO LIMITE DE FALHAS DE VENDA. RESETANDO POSIÇÃO.")
                await send_telegram_message(f"⚠️ Limite de 100 falhas de venda para **{symbol}** atingido. Posição abandonada para evitar loop.")
                in_position = False; entry_price = 0.0; automation_state["position_opened_timestamp"] = 0
                
                if automation_state.get('current_target_pair_address'):
                    automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                    automation_state["current_target_pair_address"] = None
                    await send_telegram_message(f"⚠️ **{symbol}** foi penalizada por 10 ciclos após falhas de venda.")
                
                sell_fail_count = 0
            await send_telegram_message(f"❌ FALHA NA VENDA do token {symbol}. Tentativa {sell_fail_count}/100. O bot tentará novamente.")

    except Exception as e:
        logger.error(f"Erro crítico ao vender {symbol}: {e}")
        sell_fail_count += 1
        if sell_fail_count >= 100:
            logger.error("ATINGIDO LIMITE DE FALHAS DE VENDA. RESETANDO POSIÇÃO.")
            await send_telegram_message(f"⚠️ Limite de 100 falhas de venda para **{symbol}** atingido. Posição abandonada para evitar loop.")
            in_position = False; entry_price = 0.0; automation_state["position_opened_timestamp"] = 0
            
            if automation_state.get('current_target_pair_address'):
                automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                automation_state["current_target_pair_address"] = None
                await send_telegram_message(f"⚠️ Erro crítico ao vender {symbol}: {e}. Tentativa {sell_fail_count}/100. O bot permanecerá em posição.")


# --- Funções de Análise e Descoberta ---
async def fetch_geckoterminal_ohlcv(pair_address, timeframe, limit=60):
    timeframe_map = {"1m": "minute"}
    gt_timeframe = timeframe_map.get(timeframe)
    if not gt_timeframe: return None
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair_address}/ohlcv/{gt_timeframe}?aggregate=1&limit={limit}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            res.raise_for_status()
            data = res.json()
            if data.get('data') and data['data'].get('attributes', {}).get('ohlcv_list'):
                df = pd.DataFrame(data['data']['attributes']['ohlcv_list'], columns=['ts', 'o', 'h', 'l', 'c', 'v'])
                df[['o', 'h', 'l', 'c', 'v']] = df[['o', 'h', 'l', 'c', 'v']].apply(pd.to_numeric)
                df.rename(columns={'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'}, inplace=True)
                return df
        return None
    except Exception: return None

async def fetch_dexscreener_real_time_price(pair_address):
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=5.0)
            res.raise_for_status()
            pair_data = res.json().get('pair')
            if pair_data:
                return float(pair_data.get('priceNative', 0)), float(pair_data.get('priceUsd', 0))
        return None, None
    except Exception: return None, None

async def get_pair_details(pair_address):
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            res.raise_for_status()
            pair_data = res.json().get('pair')
            if not pair_data: return None
            return {"pair_address": pair_data['pairAddress'], "base_symbol": pair_data['baseToken']['symbol'], "quote_symbol": pair_data['quoteToken']['symbol'], "base_address": pair_data['baseToken']['address'], "quote_address": pair_data['quoteToken']['address']}
    except Exception: return None
    
async def is_pair_quotable_on_jupiter(pair_details):
    if not pair_details: return False
    test_amount_wei = 10000
    url = f"https://quote-api.jup.ag/v6/quote?inputMint={pair_details['quote_address']}&outputMint={pair_details['base_address']}&amount={test_amount_wei}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            return res.status_code == 200
    except Exception:
        return False

async def calculate_dynamic_slippage(pair_address):
    logger.info(f"Calculando slippage dinâmico para {pair_address} com base na volatilidade...")
    df = await fetch_geckoterminal_ohlcv(pair_address, "1m", limit=5)
    if df is None or df.empty or len(df) < 5:
        logger.warning("Dados insuficientes. Usando slippage padrão (5.0%).")
        return 500
    price_range = df['high'].max() - df['low'].min()
    volatility = (price_range / df['low'].min()) * 100 if df['low'].min() > 0 else 0
    if volatility > 10.0:
        slippage_bps = 1000 # 10%
    else:
        slippage_bps = 500  # 5%
    logger.info(f"Volatilidade ({volatility:.2f}%). Slippage definido para {slippage_bps/100:.2f}%.")
    return slippage_bps

# --- FUNÇÃO DE DESCOBERTA ATUALIZADA ---
async def discover_and_filter_pairs():
    logger.info("--- FASE 1: DESCOBERTA --- Buscando os top 200 pares no GeckoTerminal...")
    all_pools = []
    
    for page in range(1, 11):
        url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools?page={page}&include=base_token,quote_token"
        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(url, timeout=20.0)
                res.raise_for_status()
                pools_data = res.json().get('data', [])
                if not pools_data: break
                all_pools.extend(pools_data)
                logger.info(f"Página {page} processada, {len(all_pools)} pares acumulados.")
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Erro ao buscar página {page} no GeckoTerminal: {e}"); break
    
    filtered_pairs = {}
    logger.info(f"Encontrados {len(all_pools)} pares populares. Aplicando filtros...")
    
    for pool in all_pools:
        rejection_reasons = []
        try:
            attr = pool.get('attributes', {})
            relationships = pool.get('relationships', {})
            symbol = attr.get('name', 'N/A').split(' / ')[0]
            address = pool.get('id', 'N/A')
            if address.startswith("solana_"): address = address.split('_')[1]

            is_sol_pair = False
            quote_token_addr = relationships.get('quote_token', {}).get('data', {}).get('id')
            if quote_token_addr == 'So11111111111111111111111111111111111111112' or attr.get('name', '').endswith(' / SOL'):
                is_sol_pair = True
            if not is_sol_pair: rejection_reasons.append("Não é par contra SOL")
            
            liquidity = float(attr.get('reserve_in_usd', 0))
            if liquidity < 50000: rejection_reasons.append(f"Liquidez Baixa (${liquidity:,.0f})")

            volume_24h = float(attr.get('volume_usd', {}).get('h24', 0))
            if volume_24h < 250000: rejection_reasons.append(f"Volume 24h Baixo (${volume_24h:,.0f})")

            age_str = attr.get('pool_created_at')
            if age_str:
                age_dt = datetime.fromisoformat(age_str.replace('Z', '+00:00'))
                age_hours = (datetime.now(timezone.utc) - age_
