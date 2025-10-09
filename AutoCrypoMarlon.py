# sniper_bot.py
import os
import time
import logging
import asyncio
from datetime import datetime, timezone
from base64 import b64decode
import httpx
import pandas as pd
from collections import Counter

from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned

from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from spl.token.instructions import get_associated_token_address

from telegram.ext import Application, CommandHandler
import telegram

from flask import Flask
from threading import Thread

# --- CÓDIGO DO SERVIDOR WEB ---
app = Flask('')
@app.route('/')
def home():
    return "Bot is alive!"
def run_server():
  app.run(host='0.0.0.0',port=8000)
def keep_alive():
    t = Thread(target=run_server)
    t.start()
# --- FIM DO CÓDIGO DO SERVIDOR ---

# ---------------- Configuração ----------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("sniper_bot")

# Variáveis de ambiente (Koyeb)
# --- Configurações Iniciais ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
PRIVATE_KEY_B58 = os.getenv("PRIVATE_KEY_BASE58")
RPC_URL = os.getenv("RPC_URL")

MIN_LIQUIDITY = 40000  # Mínimo de $40,000 de liquidez
MIN_VOLUME_H1 = 100000 # Mínimo de $100,000 de volume na última hora
TRADE_INTERVAL_SECONDS = 30

if not all([RPC_URL, PRIVATE_KEY_B58, TELEGRAM_TOKEN, CHAT_ID]):
    logger.error("Erro: variáveis de ambiente RPC_URL, PRIVATE_KEY_B58, TELEGRAM_TOKEN e CHAT_ID são obrigatórias.")
    raise SystemExit(1)

try:
    CHAT_ID = int(CHAT_ID)
except Exception:
    logger.error("CHAT_ID deve ser um inteiro (ID do chat).")
    raise SystemExit(1)

# Solana client (sync) e payer (a partir do Base58 private key que você informou)
solana_client = Client(RPC_URL)
try:
    payer = Keypair.from_base58_string(PRIVATE_KEY_B58)
    logger.info(f"Carteira carregada. Pubkey: {payer.pubkey()}")
except Exception as e:
    logger.error(f"Erro ao carregar private key: {e}")
    raise

# ---------------- Estado global ----------------
application = None

bot_running = False
periodic_task = None

in_position = False
entry_price = 0.0

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
    "took_profit_pairs": set(),
    "checking_volatility": False,
    "volatility_check_start_time": 0
}

parameters = {
    "timeframe": "1m",
    "amount": None,                 # em SOL
    "stop_loss_percent": None,      # ex: 15
    "take_profit_percent": None,    # ex: 20
    "priority_fee": 2000000
}

# ---------------- Utilitários Telegram ----------------
async def send_telegram_message(message):
    """Envia mensagem para o chat configurado (usa application global)."""
    if application:
        try:
            await application.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')
        except telegram.error.RetryAfter as e:
            logger.warning(f"Telegram flood control: aguardando {e.retry_after}s")
            await asyncio.sleep(e.retry_after)
            try:
                await application.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')
            except Exception as e2:
                logger.error(f"Falha ao reenviar mensagem ao Telegram: {e2}")
        except Exception as e:
            logger.error(f"Erro ao enviar mensagem para Telegram: {e}")
    else:
        logger.info("application não inicializado; mensagem Telegram não enviada.")

# ---------------- Funções de swap / slippage (seu código) ----------------
async def execute_swap(input_mint_str, output_mint_str, amount, input_decimals, slippage_bps):
    logger.info(f"Iniciando swap de {amount} do token {input_mint_str} para {output_mint_str} com slippage de {slippage_bps} BPS e limite computacional dinâmico.")
    amount_wei = int(amount * (10**input_decimals))
    
    async with httpx.AsyncClient() as client:
        try:
            quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint_str}&outputMint={output_mint_str}&amount={amount_wei}&slippageBps={slippage_bps}&maxAccounts=64"
            quote_res = await client.get(quote_url, timeout=60.0)
            quote_res.raise_for_status()
            quote_response = quote_res.json()

            swap_payload = { 
                "userPublicKey": str(payer.pubkey()), 
                "quoteResponse": quote_response, 
                "wrapAndUnwrapSol": True, 
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": {
                    "priorityLevelWithMaxLamports": {
                        "maxLamports": 10000000,
                        "priorityLevel": "veryHigh"
                    }
                }
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
            # assinatura com solders - use to_bytes_versioned
            signature = payer.sign_message(to_bytes_versioned(swap_tx.message))
            signed_tx = VersionedTransaction.populate(swap_tx.message, [signature])

            tx_opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
            # send_raw_transaction espera bytes
            tx_signature = solana_client.send_raw_transaction(bytes(signed_tx), opts=tx_opts).value
            
            logger.info(f"Transação enviada: {tx_signature}")
            
            # tentativa de confirmação
            try:
                solana_client.confirm_transaction(tx_signature, commitment="confirmed")
                logger.info(f"Transação confirmada: https://solscan.io/tx/{tx_signature}")
                return str(tx_signature)
            except Exception as confirm_e:
                logger.error(f"Transação enviada, mas falha na confirmação: {confirm_e}")
                return None

        except Exception as e:
            logger.error(f"Falha na transação: {e}")
            await send_telegram_message(f"⚠️ Falha na transação: {e}")
            return None

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

# ---------------- Funções de dados reais (Dexscreener / Geckoterminal) ----------------
async def get_pair_details(pair_address):
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            res.raise_for_status()
            pair_data = res.json().get('pair')
            if not pair_data: return None
            return {
                "pair_address": pair_data['pairAddress'],
                "base_symbol": pair_data['baseToken']['symbol'],
                "quote_symbol": pair_data['quoteToken']['symbol'],
                "base_address": pair_data['baseToken']['address'],
                "quote_address": pair_data['quoteToken']['address']
            }
    except Exception:
        return None

async def fetch_geckoterminal_ohlcv(pair_address, timeframe, limit=60):
    # timeframe map (apenas 1m implementado)
    timeframe_map = {"1m": "minute", "5m": "minute"}  # para 5m tratamos agregando candles mais tarde se necessário
    gt_timeframe = timeframe_map.get(timeframe, "minute")
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair_address}/ohlcv/{gt_timeframe}?aggregate=1&limit={limit}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            res.raise_for_status()
            data = res.json()
            if data.get('data') and data['data'].get('attributes', {}).get('ohlcv_list'):
                ohlcv = data['data']['attributes']['ohlcv_list']
                df = pd.DataFrame(ohlcv, columns=['ts','o','h','l','c','v'])
                df[['o','h','l','c','v']] = df[['o','h','l','c','v']].apply(pd.to_numeric)
                df.rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'}, inplace=True)
                return df.tail(limit).reset_index(drop=True)
    except Exception:
        return None

async def fetch_dexscreener_real_time_price(pair_address):
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=5.0)
            res.raise_for_status()
            pair_data = res.json().get('pair')
            if pair_data:
                # priceNative and priceUsd available depending on the pair
                native = pair_data.get('priceNative')
                usd = pair_data.get('priceUsd')
                try:
                    return float(native if native is not None else 0), float(usd if usd is not None else 0)
                except:
                    return None, None
            return None, None
    except Exception:
        return None, None

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

# ---------------- Descoberta e Filtragem (implementado conforme solicitado) ----------------
async def get_new_pools(page=1):
    """Busca uma página de novas pools da API GeckoTerminal."""
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools?page={page}&sort=h24_volume_usd_desc&include=base_token,quote_token"
    headers = {'Accept': 'application/json'}
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, headers=headers, timeout=10.0)
            resp.raise_for_status() # Lança um erro para respostas 4xx/5xx
            data = resp.json()
            return data.get('data', [])
        except httpx.HTTPStatusError as e:
            logger.error(f"Erro de status da API ao buscar pools: {e.response.status_code} - {e.response.text}")
        except httpx.RequestError as e:
            logger.error(f"Erro de requisição ao buscar pools: {e}")
        except Exception as e:
            logger.error(f"Erro inesperado ao buscar pools: {e}")
        return []

import time
import requests
from datetime import datetime, timedelta

import time
import requests
from datetime import datetime

def discover_and_filter_pairs(pages_to_scan=1):
    print(f"Iniciando a busca por novas moedas na rede Solana, escaneando {pages_to_scan} página(s)...")
    
    filtered_pairs = []
    
    for page in range(1, pages_to_scan + 1):
        url = f"https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page={page}"
        headers = {'Content-Type': 'application/json'}
        
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            
            pairs = data['data']
            
            if not pairs:
                print(f"Nenhum par novo encontrado na página {page}. Encerrando a busca...")
                break

            for pair in pairs:
                attributes = pair['attributes']
                
                # Nome e endereço do par para logs
                pair_name = attributes.get('name', 'N/A')
                pair_address = attributes['address']
                
                # Verificação de valores nulos para evitar erros
                created_at_str = attributes['pool_created_at']
                if created_at_str is None or attributes.get('reserve_in_usd') is None or attributes.get('transactions') is None:
                    print(f"❌ Par {pair_name} ({pair_address}) eliminado: Dados essenciais faltando.")
                    continue
                
                created_at_ts = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                age_seconds = (datetime.now(created_at_ts.tzinfo) - created_at_ts).total_seconds()
                
                # Filtro 1: Idade da pool (1 a 10 minutos)
                if not (60 <= age_seconds <= 900):
                    print(f"❌ Par {pair_name} ({pair_address}) eliminado: Fora da janela de idade (Idade: {age_seconds:.2f}s).")
                    continue
                
                try:
                    liquidity_usd = float(attributes['reserve_in_usd'])
                    txns_h1_buys = attributes['transactions']['h1']['buys']
                    txns_h1_sells = attributes['transactions']['h1']['sells']
                    price_change_h1 = float(attributes['price_change_percentage']['h1'])
                    volume_h1_usd = float(attributes['volume_usd']['h1'])
                except (KeyError, TypeError, ValueError):
                    print(f"❌ Par {pair_name} ({pair_address}) eliminado: Erro ao converter dados.")
                    continue

                # Filtro 2: Liquidez mínima
                if liquidity_usd < 50000:
                    print(f"❌ Par {pair_name} ({pair_address}) eliminado: Baixa liquidez (USD: {liquidity_usd:,.2f}).")
                    continue

                # Filtro 3: Taxa de vendas vs. compras (AGORA MAIS FLEXÍVEL)
                if txns_h1_buys > 0 and (txns_h1_sells / txns_h1_buys) > 0.5:
                    print(f"❌ Par {pair_name} ({pair_address}) eliminado: Proporção de vendas muito alta ({txns_h1_sells} vendas, {txns_h1_buys} compras).")
                    continue

                # Filtro 4: Volume e variação de preço
                if volume_h1_usd < 100000 or price_change_h1 < 20:
                    print(f"❌ Par {pair_name} ({pair_address}) eliminado: Volume/Variação insuficientes (Volume: {volume_h1_usd:,.2f} USD, Variação: {price_change_h1:.2f}%).")
                    continue
                
                pair['pair_address'] = pair_address
                filtered_pairs.append(pair)
                print(f"✅ Nova moeda encontrada e validada na Solana: {pair_name} - Endereço: {pair_address}")

        except requests.exceptions.RequestException as e:
            print(f"Erro na requisição para a API do GeckoTerminal (Página {page}): {e}")
            break

    return filtered_pairs
        
def analyze_and_score_coin(pair):
    """
    Analisa e pontua uma moeda com base em dados de volume, preço e transações.
    Uma pontuação mais alta indica um potencial de momentum maior.
    """
    attributes = pair['attributes']
    
    try:
        # Extraindo dados para a análise
        volume_h1_usd = float(attributes['volume_usd']['h1'])
        price_change_h1 = float(attributes['price_change_percentage']['h1'])
        txns_h1_buys = attributes['transactions']['h1']['buys']
        txns_h1_sells = attributes['transactions']['h1']['sells']
        
        # --- Lógica de Pontuação ---
        # Pontuação 1: Volume
        volume_score = 0
        if volume_h1_usd >= 300000:
            volume_score = 40
        elif volume_h1_usd >= 100000:
            volume_score = 30
        elif volume_h1_usd >= 50000:
            volume_score = 15

        # Pontuação 2: Variação de Preço
        price_change_score = 0
        if price_change_h1 >= 500:
            price_change_score = 30
        elif price_change_h1 >= 200:
            price_change_score = 20
        elif price_change_h1 >= 50:
            price_change_score = 10
            
        # Pontuação 3: Compras vs. Vendas
        buys_sells_score = 0
        if txns_h1_buys > 0:
            buy_ratio = txns_h1_buys / (txns_h1_buys + txns_h1_sells)
            if buy_ratio >= 0.9: # 90% ou mais de compras
                buys_sells_score = 30
            elif buy_ratio >= 0.8: # 80% ou mais de compras
                buys_sells_score = 20
            elif buy_ratio >= 0.7: # 70% ou mais de compras
                buys_sells_score = 10
        
        # Calculando a pontuação final
        final_score = volume_score + price_change_score + buys_sells_score
        
        print(f"Análise de {attributes['name']}:")
        print(f"  Volume (H1): ${volume_h1_usd:,.2f} -> Pontos: {volume_score}")
        print(f"  Variação (H1): {price_change_h1:.2f}% -> Pontos: {price_change_score}")
        print(f"  Compras/Vendas: {buy_ratio:.2f} -> Pontos: {buys_sells_score}")
        print(f"  Pontuação Final: {final_score:.2f}\n")
        
        return final_score
    
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
        print(f"Erro ao analisar a moeda {attributes.get('name', 'N/A')}: {e}")
        return 0
        
async def find_best_coin_to_trade(pair_info):
    """
    Analisa uma única moeda aprovada, calcula sua pontuação e retorna seus detalhes.
    """
    if not pair_info or 'address' not in pair_info:
        logger.warning("Nenhuma informação de par válida recebida para análise.")
        return None, {}

    symbol = pair_info.get('symbol', 'N/A')
    address = pair_info.get('address')
    
    logger.info(f"🔎 Analisando o par aprovado: {symbol} ({address})")

    # CORREÇÃO: Chamando a função de pontuação com o nome correto ('analyze_and_score_coin')
    # e passando os argumentos que ela espera (symbol, address).
    score = await analyze_and_score_coin(symbol, address)
    
    # A função original retorna 0 em caso de erro.
    if score is None:
        logger.error(f"Não foi possível calcular a pontuação para {symbol}. Descartando.")
        return None, {}

    logger.info(f"🏆 Par único analisado: {symbol} (Score={score:.2f})")

    try:
        # A função get_pair_details já existe e busca os detalhes completos
        details = await get_pair_details(address)
        if not details:
            logger.error(f"Não foi possível obter os detalhes completos para o par {symbol} no endereço {address}")
            return None, {}
            
        details['score'] = score
        best_pair_symbol = details.get('base_symbol', symbol)
        
        return best_pair_symbol, details

    except Exception as e:
        logger.error(f"Erro crítico ao obter detalhes finais para {symbol}: {e}", exc_info=True)
        return None, {}

    
# ---------------- Ordem: BUY / SELL (usando seu código) ----------------
async def execute_buy_order(amount, price, pair_details, manual=False, reason="Sinal da Estratégia"):
    global in_position, entry_price, sell_fail_count, buy_fail_count
    if in_position: 
        logger.info("Já em posição, abortando compra.")
        return

    if not manual:
        logger.info(f"Verificação final de cotação para {pair_details['base_symbol']} antes da compra...")
        if not await is_pair_quotable_on_jupiter(pair_details):
            logger.error(f"FALHA NA COMPRA: Par {pair_details['base_symbol']} não mais negociável na Jupiter. Penalizando e buscando novo alvo.")
            await send_telegram_message(f"❌ Compra para **{pair_details['base_symbol']}** abortada. Moeda não mais negociável na Jupiter.")
            if automation_state.get("current_target_pair_address"):
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
        logger.error(f"FALHA NA EXECUÇÃO da compra para {pair_details['base_symbol']}. Tentativa {buy_fail_count}/10.")
        if buy_fail_count >= 10:
            logger.error(f"Limite de falhas de compra atingido para {pair_details['base_symbol']}. Penalizando.")
            await send_telegram_message(f"❌ FALHA NA EXECUÇÃO da compra para **{pair_details['base_symbol']}**. Limite atingido. Moeda penalizada.")
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
                await send_telegram_message(f"⚠️ Limite de 100 falhas de venda para **{symbol}** atingido. Posição abandonada.")
                in_position = False; entry_price = 0.0; automation_state["position_opened_timestamp"] = 0
                if automation_state.get('current_target_pair_address'):
                    automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                    automation_state["current_target_pair_address"] = None
                    await send_telegram_message(f"⚠️ **{symbol}** foi penalizada por 10 ciclos após falhas de venda.")
                sell_fail_count = 0
            await send_telegram_message(f"⚠️ Erro ao obter saldo do token {symbol}. A venda falhou. Tentativa {sell_fail_count}/100.")
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
            logger.error(f"FALHA NA VENDA do token {symbol}. Tentativa {sell_fail_count+1}/100.")
            sell_fail_count += 1
            if sell_fail_count >= 100:
                logger.error("ATINGIDO LIMITE DE FALHAS DE VENDA. RESETANDO POSIÇÃO.")
                await send_telegram_message(f"⚠️ Limite de 100 falhas de venda para **{symbol}** atingido. Posição abandonada.")
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
            await send_telegram_message(f"⚠️ Limite de 100 falhas de venda para **{symbol}** atingido. Posição abandonada.")
            in_position = False; entry_price = 0.0; automation_state["position_opened_timestamp"] = 0
            if automation_state.get('current_target_pair_address'):
                automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                automation_state["current_target_pair_address"] = None
                await send_telegram_message(f"⚠️ Erro crítico ao vender {symbol}: {e}. Tentativa {sell_fail_count}/100. O bot permanecerá em posição.")

# ---------------- Estratégia velocity / momentum & adaptive timeout ----------------
async def check_velocity_strategy():
    """Analisa o alvo atual e gerencia o processo de compra em múltiplos estágios, com logs detalhados."""
    global in_position, automation_state, pair_details
    target_address = automation_state.get("current_target_pair_address")

    if not target_address or in_position:
        return

    pair_details = automation_state.get("current_target_pair_details", {})
    symbol = pair_details.get('base_symbol', 'N/A')
    now = time.time()

    try:
        # ----------------------------------------------------
        # FASE 2: OBSERVAÇÃO DE VOLATILIDADE (3 minutos)
        # ----------------------------------------------------
        if automation_state.get("checking_volatility"):
            tempo_restante = 180 - (now - automation_state.get("volatility_check_start_time", 0))
            logger.info(f"⏳ Observando {symbol} por mais {tempo_restante:.0f}s para garantir estabilidade...")
            
            data = await fetch_geckoterminal_ohlcv(target_address, "1m", limit=1)
            if data is not None and not data.empty:
                price_change_pct = ((data['close'].iloc[-1] - data['open'].iloc[0]) / data['open'].iloc[0]) * 100
                logger.info(f"   (Variação da vela atual: {price_change_pct:+.2f}%)")
                
                if abs(price_change_pct) > 10.0:
                    msg = f"⚠️ Volatilidade extrema detectada para **{symbol}** (+/-10%). Alvo penalizado. Reiniciando caça."
                    logger.warning(msg.replace("**",""))
                    await send_telegram_message(msg)
                    automation_state["penalty_box"][target_address] = 10
                    automation_state["current_target_pair_address"] = None
                    automation_state["checking_volatility"] = False
                    return

            if now - automation_state.get("volatility_check_start_time", 0) > 180:
                automation_state["checking_volatility"] = False
                automation_state["volatility_check_passed"] = True
                msg = f"✅ Estabilidade de **{symbol}** confirmada. Aguardando gatilho final de compra (>+2%)."
                logger.info(msg.replace("**",""))
                await send_telegram_message(msg)
            return

        # ----------------------------------------------------
        # FASE 3: AGUARDANDO GATILHO FINAL DE COMPRA
        # ----------------------------------------------------
        if automation_state.get("volatility_check_passed"):
            logger.info(f"🎯 Aguardando gatilho final de compra para {symbol} (>+2% na vela de 1min)...")
            data = await fetch_geckoterminal_ohlcv(target_address, "1m", limit=1)
            if data is not None and not data.empty:
                price_change_pct = ((data['close'].iloc[-1] - data['open'].iloc[0]) / data['open'].iloc[0]) * 100
                logger.info(f"   (Variação da vela atual: {price_change_pct:+.2f}%)")
                
                if price_change_pct > 2.0:
                    msg = f"✅ GATILHO FINAL ATINGIDO para **{symbol}**! Executando ordem de compra..."
                    logger.info(msg.replace("**",""))
                    await send_telegram_message(msg)
                    await execute_buy_order()
            return

        # ----------------------------------------------------
        # FASE 1: BUSCANDO SINAL INICIAL
        # ----------------------------------------------------
        df_1m = await fetch_geckoterminal_ohlcv(target_address, "1m", limit=1)
        df_10m = await fetch_geckoterminal_ohlcv(target_address, "1m", limit=10)
        
        if df_1m is None or df_10m is None or df_1m.empty or df_10m.empty:
            logger.warning(f"Não foi possível obter dados OHLCV para {symbol}. Tentando novamente no próximo ciclo.")
            return

        price_change_pct = ((df_1m['close'].iloc[-1] - df_1m['open'].iloc[0]) / df_1m['open'].iloc[0]) * 100
        trend10_ok = df_10m['close'].iloc[-1] > df_10m['close'].iloc[0]

        logger.info(f"🕵️ Buscando sinal inicial para {symbol}: Vela 1min={price_change_pct:+.2f}%, Tendência 10min={'Positiva' if trend10_ok else 'Negativa'}")

        if price_change_pct > 2.0 and trend10_ok:
            automation_state["checking_volatility"] = True
            automation_state["volatility_check_start_time"] = now
            msg = f"🔔 SINAL INICIAL FORTE DETECTADO para **{symbol}**. Iniciando período de observação de 3 minutos."
            logger.info(msg.replace("**",""))
            await send_telegram_message(msg)
        else:
            logger.info(f"❌ Sinal inicial para {symbol} ainda não encontrado. Continuará monitorando.")

    except Exception as e:
        logger.error(f"Erro em check_velocity_strategy: {e}", exc_info=True)
        
# ---------------- Loop autônomo completo ----------------
async def autonomous_loop():
    """O loop principal que executa a estratégia de trade de forma autônoma, com estados de operação claros."""
    global automation_state, in_position, pair_details

    logger.info("Loop autônomo iniciado.")
    while automation_state.get("is_running", False):
        try:
            now = time.time()
            # ------------------------------------------------------------------
            # ESTADO 1: CAÇA (Nenhum alvo selecionado)
            # ------------------------------------------------------------------
            if not automation_state.get("current_target_pair_address"):
                logger.info("Iniciando ciclo de caça...")
                approved_pairs = discover_and_filter_pairs(pages_to_scan=10)

                if approved_pairs:
                    best_score = 0
                    best_pair = None

                    # Analisa e pontua todas as moedas aprovadas
                    for pair in approved_pairs:
                        score = analyze_and_score_coin(pair)
                        if score > best_score:
                            best_score = score
                            best_pair = pair
                    
                    # Se a melhor moeda tiver uma pontuação acima de 70, define como alvo
                    if best_pair and best_score >= 70:
                        pair_details = best_pair
                        automation_state["current_target_pair_details"] = best_pair
                        automation_state["current_target_pair_address"] = best_pair.get('pair_address')
                        automation_state["target_selected_timestamp"] = now

                        msg = f"🎯 **Novo Alvo:** {pair_details['attributes']['name']} (Score={best_score:.2f}). Iniciando monitoramento do gatilho..."
                        logger.info(msg.replace("**", ""))
                        await send_telegram_message(msg)
                    else:
                        msg = f"⚠️ Nenhuma moeda alcançou a pontuação mínima de 70. Reiniciando caça."
                        logger.info(msg)
                        # Não precisa de send_telegram_message para não poluir

                else:
                    logger.warning("Nenhum par novo passou nos filtros iniciais nesta rodada de caça.")
                
                # Aguarda o intervalo de caça
                await asyncio.sleep(TRADE_INTERVAL_SECONDS)

            # ------------------------------------------------------------------
            # ESTADO 2: MONITORAMENTO (Alvo selecionado, aguardando para comprar)
            # ------------------------------------------------------------------
            elif automation_state.get("current_target_pair_address") and not in_position:
                # Aqui o bot continua a lógica de monitoramento
                msg = f"⚠️ Iniciando Monitoramento"
                logger.info(msg)
                
                await check_velocity_strategy()
                await asyncio.sleep(15)

            # ------------------------------------------------------------------
            # ESTADO 3: EM POSIÇÃO (Gerenciando a compra)
            # ------------------------------------------------------------------
            elif in_position:
                # (Seu código de gerenciamento de Take Profit / Stop Loss)
                pass
                await asyncio.sleep(15)


        except asyncio.CancelledError:
            logger.info("Loop autônomo cancelado.")
            break
        except Exception as e:
            logger.error(f"Erro crítico no loop autônomo: {e}", exc_info=True)
            await asyncio.sleep(60)
            
# ---------------- Comandos Telegram ----------------
async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Bot sniper iniciado.\nUse `/set <VALOR_SOL> <STOP_LOSS_%> <TAKE_PROFIT_%> [PRIORITY_FEE]` e depois `/run`.',
        parse_mode='Markdown'
    )

async def set_params(update, context):
    if bot_running:
        await update.effective_message.reply_text("Pare o bot com /stop antes de alterar os parâmetros."); return
    try:
        args = context.args
        amount, stop_loss, take_profit = float(args[0]), float(args[1]), float(args[2])
        priority_fee = int(args[3]) if len(args) > 3 else 2000000
        parameters.update(amount=amount, stop_loss_percent=stop_loss, take_profit_percent=take_profit, priority_fee=priority_fee)
        await update.effective_message.reply_text(f"✅ Parâmetros definidos: amount={amount} SOL, stop={stop_loss}%, take_profit={take_profit}%, priority_fee={priority_fee}")
    except Exception:
        await update.effective_message.reply_text("⚠️ Formato incorreto. Uso: `/set <VALOR> <STOP_%> <TAKE_%> [PRIORITY]`", parse_mode='Markdown')

async def run_bot(update, context):
    """Inicia o loop de trade autônomo."""
    global automation_state
    if automation_state.get("is_running", False):
        await update.effective_message.reply_text("✅ O bot já está em execução.")
        return

    automation_state["is_running"] = True
    automation_state["task"] = asyncio.create_task(autonomous_loop()) # <-- ESTA LINHA FOI RE-ADICIONADA
    
    logger.info("Bot de trade autônomo iniciado.")
    await update.effective_message.reply_text(
        "🚀 Bot de trade autônomo iniciado!\n"
        "O bot agora está monitorando novas pools com base nos seus critérios.\n"
        "Use /stop para parar."
    )

async def stop_bot(update, context):
    """Para o loop de trade autônomo e cancela a tarefa em execução."""
    global automation_state, in_position

    if not automation_state.get("is_running", False):
        await update.effective_message.reply_text("O bot já está parado."); return

    # CORREÇÃO: Desliga o bot usando a variável de estado correta
    automation_state["is_running"] = False
    
    # Cancela a tarefa asyncio se ela existir
    if "task" in automation_state and automation_state["task"]:
        automation_state["task"].cancel()

    if in_position:
        await execute_sell_order("Parada manual do bot")

    # Limpa o estado para um reinício limpo
    automation_state.update(
        current_target_pair_address=None,
        current_target_symbol=None,
        position_opened_timestamp=0,
        target_selected_timestamp=0,
        checking_volatility=False
    )
    
    logger.info("Bot de trade parado.")
    await update.effective_message.reply_text("🛑 Bot parado. Todas as tarefas e posições foram finalizadas.")

async def manual_buy(update, context):
    if not bot_running:
        await update.effective_message.reply_text("⚠️ O bot precisa estar em execução. Use /run primeiro.")
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
        price_native, _ = await fetch_dexscreener_real_time_price(pair_details['pair_address'])
        if price_native:
            await update.effective_message.reply_text(f"Forçando compra manual de {amount} SOL em {pair_details['base_symbol']}...")
            await execute_buy_order(amount, price_native, pair_details, manual=True, reason="Compra Manual Forçada")
        else:
            await update.effective_message.reply_text("⚠️ Não foi possível obter o preço atual para a compra.")
    except (IndexError, ValueError):
        await update.effective_message.reply_text("⚠️ Formato incorreto. Use: `/buy <VALOR>`", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Erro no comando /buy: {e}")
        await update.effective_message.reply_text(f"⚠️ Erro ao executar compra manual: {e}")

async def manual_sell(update, context):
    if not in_position:
        await update.effective_message.reply_text("⚠️ Nenhuma posição aberta para vender.")
        return
    await update.effective_message.reply_text("Forçando venda manual da posição atual...")
    await execute_sell_order(reason="Venda Manual Forçada")

# ---------------- Main ----------------
def main():
    global application
    keep_alive()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set", set_params))
    application.add_handler(CommandHandler("run", run_bot))
    application.add_handler(CommandHandler("stop", stop_bot))
    application.add_handler(CommandHandler("buy", manual_buy))
    application.add_handler(CommandHandler("sell", manual_sell))
    logger.info("Bot do Telegram iniciado e aguardando comandos...")
    application.run_polling()

if __name__ == '__main__':
    main()









































