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
                logger.error(f"Erro na API da Jupiter: {swap_response}"); return None, "Jupiter API Error"

            raw_tx_bytes = b64decode(swap_tx_b64)
            swap_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
            signature = payer.sign_message(to_bytes_versioned(swap_tx.message))
            signed_tx = VersionedTransaction.populate(swap_tx.message, [signature])

            tx_opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
            tx_signature = solana_client.send_raw_transaction(bytes(signed_tx), opts=tx_opts).value
            
            logger.info(f"Transação enviada: {tx_signature}")
            
            for _ in range(5):
                await asyncio.sleep(10)
                try:
                    tx_status = solana_client.get_transaction(tx_signature, commitment="confirmed")
                    if tx_status.value and tx_status.value.meta.err is None:
                        logger.info(f"Transação confirmada e bem-sucedida: https://solscan.io/tx/{tx_signature}")
                        return tx_signature, None
                    elif tx_status.value and tx_status.value.meta.err:
                        error_message = str(tx_status.value.meta.err)
                        logger.error(f"Transação confirmada com erro: {error_message}")
                        return tx_signature, error_message
                except Exception as status_e:
                    logger.warning(f"Aguardando confirmação... Erro temporário: {status_e}")
            
            logger.error("Transação não confirmada após várias tentativas ou falhou com erro desconhecido.")
            return None, "Transaction not confirmed or failed"

        except Exception as e:
            logger.error(f"Falha na transação: {e}"); await send_telegram_message(f"⚠️ Falha na transação: {e}"); return None, str(e)


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
    
    tx_sig, tx_error = await execute_swap(pair_details['quote_address'], pair_details['base_address'], amount, 9, slippage_bps)

    if tx_sig and not tx_error:
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
        if tx_error:
            await send_telegram_message(f"❌ FALHA NA COMPRA para **{pair_details['base_symbol']}**. Motivo: `{tx_error}`. Tentativa {buy_fail_count}/10. O bot tentará novamente.")
        
        if buy_fail_count >= 10:
            logger.error(f"FALHA NA EXECUÇÃO da compra para {pair_details['base_symbol']}. Limite de {buy_fail_count} falhas atingido. Penalizando e procurando novo alvo.")
            await send_telegram_message(f"❌ FALHA NA EXECUÇÃO da compra para **{pair_details['base_symbol']}**. Limite de 10 falhas atingido. A moeda será penalizada.")
            if automation_state.get("current_target_pair_address"):
                automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                automation_state["current_target_pair_address"] = None
            buy_fail_count = 0
        else:
            logger.error(f"FALHA NA EXECUÇÃO da compra para {pair_details['base_symbol']}. Tentativa {buy_fail_count}/10. O bot tentará novamente.")


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
        tx_sig, tx_error = await execute_swap(pair_details['base_address'], pair_details['quote_address'], amount_to_sell, token_balance_data.decimals, slippage_bps)
        
        if tx_sig and not tx_error:
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
            if tx_sig and tx_error:
                await send_telegram_message(f"❌ FALHA NA VENDA do token **{symbol}**. Motivo: `{tx_error}`. O bot permanecerá em posição. Tentativa {sell_fail_count + 1}/100. Transação: https://solscan.io/tx/{tx_sig}")
            elif not tx_sig and tx_error:
                 await send_telegram_message(f"❌ FALHA NA VENDA do token **{symbol}**. Motivo: `{tx_error}`. O bot permanecerá em posição. Tentativa {sell_fail_count + 1}/100.")
            
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
                age_hours = (datetime.now(timezone.utc) - age_dt).total_seconds() / 3600
                if age_hours < 0.5: rejection_reasons.append(f"Muito Nova ({age_hours:.2f} horas)")
            
            # --- NOVO FILTRO DE ATIVIDADE NA ÚLTIMA HORA ---
            volume_1h = float(attr.get('volume_usd', {}).get('h1', 0))
            if volume_1h < 100000:
                rejection_reasons.append(f"Volume 1h Baixo (${volume_1h:,.0f})")

            if not rejection_reasons:
                logger.info(f"✅ APROVADO (Fase 1): {symbol} | Vol 1h: ${volume_1h:,.0f}")
                filtered_pairs[symbol] = address
            else:
                logger.info(f"❌ DESCARTADO (Fase 1): {symbol} | Motivos: {', '.join(rejection_reasons)}")
                
        except (ValueError, TypeError, KeyError, IndexError):
            continue

    logger.info(f"Descoberta finalizada. {len(filtered_pairs)} pares passaram nos filtros iniciais.")
    return filtered_pairs

async def analyze_and_score_coin(pair_address, symbol):
    try:
        pair_details = await get_pair_details(pair_address)
        if not pair_details: return 0, None
        if not await is_pair_quotable_on_jupiter(pair_details):
            logger.warning(f"Candidato {symbol} descartado: Não negociável na Jupiter."); return 0, None

        df = await fetch_geckoterminal_ohlcv(pair_address, "1m", limit=15)
        if df is None or len(df) < 15:
            logger.warning(f"Dados insuficientes para {symbol}."); return 0, None
        
        if df['volume'].sum() < 500:
            logger.info(f"Candidato {symbol} descartado: Atividade Recente Baixa."); return 0, None
            
        price_range = df['high'].max() - df['low'].min()
        volatility_score = (price_range / df['low'].min()) * 100 if df['low'].min() > 0 else 0
        volume_score = df['volume'].sum()
        base_score = (volatility_score * 1000) + volume_score

        total_move = df['high'].max() - df['low'].min()
        if total_move > 0:
            df['candle_move'] = df['high'] - df['low']
            biggest_candle_move = df['candle_move'].max()
            spike_ratio = biggest_candle_move / total_move
            trend_quality_index = 1 - spike_ratio
        else:
            trend_quality_index = 0
            
        final_score = base_score * trend_quality_index
        logger.info(f"Candidato: {symbol} | Pontuação Base: {base_score:,.0f}, Qualidade: {trend_quality_index:.2f} | Pontuação Final: {final_score:,.0f}")
        return final_score, pair_details
    except Exception as e:
        logger.error(f"Erro ao analisar {symbol} ({pair_address}): {e}"); return 0, None

async def find_best_coin_to_trade(candidate_pairs, penalized_pairs=set()):
    logger.info("--- FASE 2: SELEÇÃO --- Pontuando os melhores pares...")
    if not candidate_pairs:
        logger.warning("Nenhum candidato para pontuar."); return None
        
    best_score, best_coin_info = -1, None
    valid_candidates = {s: a for s, a in candidate_pairs.items() if a not in penalized_pairs}
    if not valid_candidates:
        logger.warning("Nenhum candidato válido após remover os penalizados.")
        return None

    tasks = [analyze_and_score_coin(addr, symbol) for symbol, addr in valid_candidates.items()]
    results = await asyncio.gather(*tasks)

    for (symbol, addr), (score, details) in zip(valid_candidates.items(), results):
        if details and score > best_score:
            best_score = score
            best_coin_info = {"symbol": symbol, "pair_address": addr, "score": score, "details": details}
    
    if best_coin_info:
        logger.info(f"--- SELEÇÃO FINALIZADA --- Melhor moeda: {best_coin_info['symbol']} (Pontuação Final: {best_coin_info['score']:,.0f})")
    else:
        logger.warning("--- SELEÇÃO FINALIZADA --- Nenhuma moeda com oportunidade clara encontrada.")
    return best_coin_info

async def check_velocity_strategy():
    global in_position, entry_price
    target_address = automation_state.get("current_target_pair_address")
    if not target_address or in_position or automation_state.get("checking_volatility"): return

    pair_details = automation_state.get("current_target_pair_details")
    data = await fetch_geckoterminal_ohlcv(target_address, parameters["timeframe"], limit=2)
    if data is None or len(data) < 2: return

    last_closed_candle = data.iloc[0]
    
    price_change_pct = (last_closed_candle['close'] - last_closed_candle['open']) / last_closed_candle['open'] * 100 if last_closed_candle['open'] > 0 else 0

    logger.info(f"Análise Compra ({pair_details['base_symbol']}): "
                f"Variação Vela: {price_change_pct:+.2f}% (Meta: >2%)")

    if not automation_state.get("checking_volatility"):
        pair_details = automation_state.get("current_target_pair_details")
        logger.info(f"Moeda {pair_details['base_symbol']} selecionada. Iniciando verificação de volatilidade por 3 minutos.")
        automation_state["checking_volatility"] = True
        automation_state["volatility_check_start_time"] = time.time()
        await send_telegram_message(f"🔔 Moeda alvo **{pair_details['base_symbol']}** selecionada. Verificando volatilidade por 3 minutos antes de entrar.")

# --- Loop Principal Autônomo ---
async def autonomous_loop():
    global bot_running
    logger.info("Loop de caça autônoma iniciado.")
    while bot_running:
        try:
            now = time.time()
            force_rescan = False
            
            if not in_position and automation_state.get("current_target_pair_address") and (now - automation_state.get("target_selected_timestamp", 0) > 900):
                penalized_symbol = automation_state["current_target_symbol"]
                penalized_address = automation_state["current_target_pair_address"]
                logger.warning(f"TIMEOUT DE CAÇA: 15 min sem entrada para {penalized_symbol}. Abandonando e penalizando.")
                await send_telegram_message(f"⌛️ Timeout de caça para **{penalized_symbol}**. Procurando um novo alvo...")
                automation_state["penalty_box"][penalized_address] = 10
                automation_state["current_target_pair_address"] = None
                automation_state["checking_volatility"] = False
                force_rescan = True

            if now - automation_state.get("last_scan_timestamp", 0) > 7200:
                logger.info("Timer de 2 horas atingido. Iniciando novo ciclo de descoberta.")
                force_rescan = True
            
            if force_rescan or not automation_state.get("current_target_pair_address"):
                if automation_state["penalty_box"]:
                    logger.info(f"Gerenciando caixa de penalidade: {list(automation_state['penalty_box'].keys())}")
                    for addr in list(automation_state["penalty_box"].keys()):
                        automation_state["penalty_box"][addr] -= 1
                        if automation_state["penalty_box"][addr] <= 0:
                            del automation_state["penalty_box"][addr]
                            logger.info(f"Endereço {addr} removido da caixa de penalidade.")

                discovered_pairs = await discover_and_filter_pairs()
                automation_state["discovered_pairs"] = discovered_pairs
                best_coin = await find_best_coin_to_trade(discovered_pairs, set(automation_state["penalty_box"].keys()))
                automation_state["last_scan_timestamp"] = now
                
                if best_coin:
                    if best_coin["pair_address"] != automation_state.get("current_target_pair_address"):
                        if in_position: await execute_sell_order(reason=f"Trocando para {best_coin['symbol']}")
                        automation_state.update(
                            current_target_pair_address=best_coin["pair_address"],
                            current_target_symbol=best_coin["symbol"],
                            current_target_pair_details=best_coin["details"],
                            target_selected_timestamp=now
                        )
                        automation_state["checking_volatility"] = False
                        automation_state["volatility_check_start_time"] = 0
                        await send_telegram_message(f"🎯 **Novo Alvo:** {best_coin['symbol']}. Iniciando monitoramento...")
            
            if automation_state.get("current_target_pair_address") and not in_position:
                if automation_state.get("checking_volatility"):
                    pair_details = automation_state.get("current_target_pair_details")
                    data = await fetch_geckoterminal_ohlcv(pair_details['pair_address'], parameters["timeframe"], limit=1)
                    
                    if data is not None and not data.empty:
                        last_closed_candle = data.iloc[0]
                        price_change_pct = (last_closed_candle['close'] - last_closed_candle['open']) / last_closed_candle['open'] * 100 if last_closed_candle['open'] > 0 else 0
                        
                        if abs(price_change_pct) > 10.0:
                            logger.warning(f"Volatilidade extrema detectada para {pair_details['base_symbol']}: {price_change_pct:.2f}%. Penalizando moeda.")
                            await send_telegram_message(f"⚠️ Volatilidade extrema (+/- 10%) detectada para **{pair_details['base_symbol']}**. A moeda será penalizada e um novo alvo será buscado.")
                            automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                            automation_state["current_target_pair_address"] = None
                            automation_state["checking_volatility"] = False
                            await asyncio.sleep(60)
                            continue

                        if now - automation_state.get("volatility_check_start_time", 0) > 180: # 3 minutos
                            automation_state["checking_volatility"] = False
                            logger.info(f"Verificação de volatilidade de 3 minutos concluída para {pair_details['base_symbol']}. Moeda considerada segura.")
                            await send_telegram_message(f"✅ Volatilidade de **{pair_details['base_symbol']}** dentro do limite por 3 minutos. Agora, monitorando para sinal de compra (>2%).")
                    
                    await asyncio.sleep(15)

                elif not in_position: # Se a verificação já foi concluída, entra neste bloco para monitorar a variação > 2%
                    pair_details = automation_state.get("current_target_pair_details")
                    data = await fetch_geckoterminal_ohlcv(pair_details['pair_address'], parameters["timeframe"], limit=1)
                    if data is not None and not data.empty:
                        last_closed_candle = data.iloc[0]
                        price_change_pct = (last_closed_candle['close'] - last_closed_candle['open']) / last_closed_candle['open'] * 100 if last_closed_candle['open'] > 0 else 0
                        
                        if price_change_pct > 2.0:
                            logger.info(f"Variação da vela para {pair_details['base_symbol']} voltou a ser maior que 2%. Procedendo com a compra.")
                            await send_telegram_message(f"✅ Variação da vela para **{pair_details['base_symbol']}** voltou a subir! Executando ordem de compra...")
                            price, _ = await fetch_dexscreener_real_time_price(pair_details['pair_address'])
                            if price:
                                reason = "Sinal da Estratégia retomado"
                                await execute_buy_order(parameters["amount"], price, pair_details, reason=reason)
                    
                    await asyncio.sleep(15)
            elif in_position:
                price, _ = await fetch_dexscreener_real_time_price(automation_state["current_target_pair_address"])
                if price:
                    profit = ((price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
                    logger.info(f"Posição Aberta ({automation_state['current_target_symbol']}): P/L: {profit:+.2f}%")
                    take_profit_price = entry_price * (1 + parameters["take_profit_percent"] / 100)
                    stop_loss_price = entry_price * (1 - parameters["stop_loss_percent"] / 100)
                    
                    if price >= take_profit_price: await execute_sell_order(f"Take Profit (+{parameters['take_profit_percent']}%)"); continue
                    if price <= stop_loss_price: await execute_sell_order(f"Stop Loss (-{parameters['stop_loss_percent']}%)"); continue
                    if time.time() - automation_state.get("position_opened_timestamp", 0) > 3600:
                        reason = f"Timeout de 60 minutos (P/L: {profit:+.2f}%)"
                        await execute_sell_order(reason); continue
                await asyncio.sleep(15)
            else:
                await check_velocity_strategy()
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            logger.info("Loop autônomo cancelado."); break
        except Exception as e:
            logger.error(f"Erro crítico no loop autônomo: {e}", exc_info=True); await asyncio.sleep(60)

# --- Comandos do Telegram ---
async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Sou seu bot **v20.14 (Verificação Robusta de Transação)**.\n\n'
        '**Dinâmica Autônoma:**\n'
        '1. Eu descubro os TOP 200 pares e aplico um **filtro de atividade na última hora**.\n'
        '2. A seleção usa um **Índice de Qualidade** para priorizar tendências saudáveis.\n'
        '3. Após selecionar uma moeda, verifico a volatilidade por 3 minutos. Se uma vela variar mais de 10%, a moeda é penalizada.\n'
        '4. Se a moeda passar na verificação, eu continuo a monitorar a variação da vela até ela ficar acima de 2% para comprar, ou até o timeout de 15 minutos.\n'
        '5. Agora, o bot verifica rigorosamente o sucesso de cada transação no blockchain para evitar falsos positivos.\n\n'
        '**Estratégia:** Velocidade Pura (+2% em 1 min).\n\n'
        '**Configure-me com `/set` e inicie com `/run`.**\n'
        '`/set <VALOR> <STOP_LOSS_%> <TAKE_PROFIT_%> [TAXA_PRIORIDADE]`',
        parse_mode='Markdown'
    )

async def set_params(update, context):
    if bot_running:
        await update.effective_message.reply_text("Pare o bot com /stop antes de alterar os parâmetros."); return
    try:
        args = context.args
        amount, stop_loss, take_profit = float(args[0]), float(args[1]), float(args[2])
        priority_fee = 2000000

        if len(args) > 3:
            priority_fee = int(args[3])

        if stop_loss <= 0 or take_profit <= 0:
            await update.effective_message.reply_text("⚠️ Stop/Profit devem ser > 0."); return
        
        parameters.update(amount=amount, stop_loss_percent=stop_loss, take_profit_percent=take_profit, priority_fee=priority_fee)
        if "volume_multiplier" in parameters:
            parameters["volume_multiplier"] = None

        await update.effective_message.reply_text(
            f"✅ *Parâmetros definidos!*\n"
            f"💰 *Valor por Ordem:* `{amount}` SOL\n"
            f"🛑 *Stop Loss:* `-{stop_loss}%`\n"
            f"🎯 *Take Profit:* `+{take_profit}%`\n"
            f"⚡️ *Taxa de Prioridade:* `{priority_fee}` micro-lamports\n\n"
            "Agora use `/run` para iniciar.",
            parse_mode='Markdown'
        )
    except (IndexError, ValueError):
        await update.effective_message.reply_text(
            "⚠️ *Formato incorreto.*\n"
            "Use: `/set <VALOR> <STOP> <PROFIT> [TAXA_PRIORIDADE]`\n"
            "Ex: `/set 0.1 2.0 5.0` (taxa padrão 2000000)\n"
            "Ex: `/set 0.1 2.0 5.0 15000` (taxa personalizada)\n", 
            parse_mode='Markdown'
        )

async def run_bot(update, context):
    global bot_running, periodic_task
    if parameters['amount'] is None:
        await update.effective_message.reply_text("Defina os parâmetros com /set primeiro."); return
    if bot_running:
        await update.effective_message.reply_text("O bot já está em execução."); return
    bot_running = True
    logger.info("Bot de trade autônomo iniciado.")
    await update.effective_message.reply_text("🚀 Modo Caçador de Velocidade Pura (com Filtro de Atividade) iniciado!")
    if periodic_task is None or periodic_task.done():
        periodic_task = asyncio.create_task(autonomous_loop())

async def stop_bot(update, context):
    global bot_running, periodic_task
    if not bot_running:
        await update.effective_message.reply_text("O bot já está parado."); return
    bot_running = False
    if periodic_task:
        periodic_task.cancel()
        periodic_task = None
    if in_position:
        await execute_sell_order("Parada manual do bot")
    automation_state.update(current_target_pair_address=None, current_target_symbol=None, last_scan_timestamp=0, position_opened_timestamp=0, target_selected_timestamp=0, penalty_box={}, checking_volatility=False, volatility_check_start_time=0)
    logger.info("Bot de trade parado.")
    await update.effective_message.reply_text("🛑 Bot parado. Todas as tarefas e posições foram finalizadas.")

async def manual_buy(update, context):
    if not bot_running:
        await update.effective_message.reply_text("⚠️ O bot precisa de estar em execução. Use `/run` primeiro.")
        return
    if in_position:
        await update.effective_message.reply_text("⚠️ Já existe uma posição aberta.")
        return
    if not automation_state.get("current_target_pair_address"):
        await update.effective_message.reply_text("⚠️ O bot ainda não selecionou um alvo. Aguarde o ciclo de descoberta.")
        return
    try:
        amount = float(context.args[0])
        if amount <= 0:
            await update.effective_message.reply_text("⚠️ O valor da compra deve ser positivo.")
            return

        pair_details = automation_state["current_target_pair_details"]
        price, _ = await fetch_dexscreener_real_time_price(pair_details['pair_address'])
        if price:
            await update.effective_
