# sniper_bot.py
import os
import time
import logging
import asyncio
from base64 import b64decode
import httpx
import pandas as pd

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
  app.run(host='0.0.0.0',port=8080)
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
async def discover_and_filter_pairs():
    """
    Descobre pares no GeckoTerminal (páginas sequenciais) e aplica filtros:
    - Vida entre 15 min (900s) e 60 min (3600s)
    - Liquidez >= 40_000 USD
    - Volume 1h >= 100_000 USD
    - Não entrou antes (took_profit_pairs)
    Retorna dict {symbol: pair_address}
    """
    filtered_pairs = {}
    all_pools = []
    async with httpx.AsyncClient() as client:
        for page in range(1, 11):  # até 200+ pares (10 páginas)
            try:
                url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools?page={page}&include=base_token,quote_token"
                res = await client.get(url, timeout=20.0)
                res.raise_for_status()
                data = res.json().get('data', [])
                if not data:
                    break
                all_pools.extend(data)
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.error(f"Erro ao buscar página {page} do GeckoTerminal: {e}")
                break

    logger.info(f"Total pares buscados: {len(all_pools)}. Aplicando filtros...")
    for pool in all_pools:
        try:
            attr = pool.get('attributes', {})
            relationships = pool.get('relationships', {})
            symbol = attr.get('name', 'N/A').split(' / ')[0]
            address = pool.get('id', 'N/A')
            if address.startswith("solana_"): address = address.split('_')[1]

            # vida da pool
            age_str = attr.get('pool_created_at') or attr.get('created_at') or attr.get('createdAt')
            age_seconds = None
            if age_str:
                try:
                    age_dt = pd.to_datetime(age_str).tz_convert('UTC')
                    age_seconds = (pd.Timestamp.now(tz='UTC') - age_dt).total_seconds()
                except Exception:
                    try:
                        # fallback parse iso
                        age_dt = pd.to_datetime(age_str)
                        age_seconds = (pd.Timestamp.now(tz='UTC') - age_dt).total_seconds()
                    except Exception:
                        age_seconds = None

            # filtra vida
            if age_seconds is None or age_seconds < 900 or age_seconds > 3600:
                continue

            # liquidez
            liquidity = float(attr.get('reserve_in_usd', attr.get('liquidity_usd', 0) or 0))
            if liquidity < 40000:
                continue

            # volume 1h
            volume_1h = float(attr.get('volume_usd', {}).get('h1', 0) or 0)
            if volume_1h < 100000:
                continue

            # somente pares contra SOL (preferência) - opcional
            quote_token_id = relationships.get('quote_token', {}).get('data', {}).get('id')
            if quote_token_id != 'So11111111111111111111111111111111111111112' and not attr.get('name','').endswith(' / SOL'):
                # aceitar só pares contra SOL (reduz ruído)
                continue

            # não reentrar se já take profit
            if address in automation_state['took_profit_pairs']:
                continue

            logger.info(f"✅ APROVADO: {symbol} | Liquidez ${liquidity:,.0f} | Vol1h ${volume_1h:,.0f}")
            filtered_pairs[symbol] = address

        except Exception:
            continue

    logger.info(f"Descoberta finalizada. {len(filtered_pairs)} pares passaram nos filtros iniciais.")
    return filtered_pairs

# ---------------- Scoring / análise ----------------
async def analyze_and_score_coin(pair_address, symbol):
    try:
        pair_details = await get_pair_details(pair_address)
        if not pair_details: return 0, None
        if not await is_pair_quotable_on_jupiter(pair_details): 
            logger.info(f"{symbol} não quotable em Jupiter -> descartado")
            return 0, None

        df_1m = await fetch_geckoterminal_ohlcv(pair_address, "1m", limit=15)
        df_5m = await fetch_geckoterminal_ohlcv(pair_address, "1m", limit=15)  # we'll aggregate for 5m
        if df_1m is None or len(df_1m) < 15:
            logger.info(f"{symbol} sem dados 1m suficientes")
            return 0, None

        # build 5m trend by resampling 1m (if available)
        try:
            df_1m['ts_dt'] = pd.to_datetime(df_1m['ts'], unit='s')
            df_1m = df_1m.set_index('ts_dt')
            df_5m_series = df_1m['close'].resample('5T').last().dropna()
            if len(df_5m_series) < 2:
                return 0, None
            trend5 = df_5m_series.iloc[-1] - df_5m_series.iloc[0]
            if trend5 <= 0:
                logger.info(f"{symbol} sem tendência 5m positiva -> descartado")
                return 0, None
        except Exception:
            # se falhar, aborta
            return 0, None

        # volume and volatility scoring
        vol_sum = df_1m['volume'].sum()
        price_range = df_1m['high'].max() - df_1m['low'].min()
        low_min = df_1m['low'].min()
        volatility_score = (price_range / low_min) * 100 if low_min and low_min > 0 else 0
        base_score = (volatility_score * 1000) + vol_sum

        total_move = price_range
        if total_move > 0:
            df_1m['candle_move'] = df_1m['high'] - df_1m['low']
            biggest = df_1m['candle_move'].max()
            spike_ratio = biggest / total_move if total_move else 1
            trend_quality_index = max(0.0, 1 - spike_ratio)
        else:
            trend_quality_index = 0

        final_score = base_score * trend_quality_index
        logger.info(f"Candidato {symbol} -> score {final_score:,.0f} (vol {volatility_score:.2f}%, vol_sum {vol_sum:,.0f})")
        return final_score, pair_details

    except Exception as e:
        logger.error(f"Erro ao analisar {symbol} ({pair_address}): {e}")
        return 0, None

async def find_best_coin_to_trade(candidate_pairs, penalized_pairs=set()):
    if not candidate_pairs:
        logger.info("Nenhum candidato para pontuar.")
        return None
    best_score, best_coin_info = -1, None
    valid_candidates = {s:a for s,a in candidate_pairs.items() if a not in penalized_pairs}
    if not valid_candidates:
        logger.info("Nenhum candidato válido após penalizados.")
        return None

    tasks = [analyze_and_score_coin(addr, symbol) for symbol, addr in valid_candidates.items()]
    results = await asyncio.gather(*tasks)
    for (symbol, addr), (score, details) in zip(valid_candidates.items(), results):
        if details and score > best_score:
            best_score = score
            best_coin_info = {"symbol": symbol, "pair_address": addr, "score": score, "details": details}
    if best_coin_info:
        logger.info(f"Selecionado: {best_coin_info['symbol']} (score {best_coin_info['score']:,.0f})")
    return best_coin_info

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
    """Analisa candles 1m do target e decide entrar se vela > +2% e 5m em tendência positiva."""
    global in_position
    target_address = automation_state.get("current_target_pair_address")
    if not target_address or in_position or automation_state.get("checking_volatility"):
        return

    pair_details = automation_state.get("current_target_pair_details")
    data = await fetch_geckoterminal_ohlcv(target_address, parameters["timeframe"], limit=5)
    if data is None or len(data) < 2:
        return

    last = data.iloc[-1]
    price_change_pct = (last['close'] - last['open']) / last['open'] * 100 if last['open'] > 0 else 0

    # check 5m trend
    df_5m = await fetch_geckoterminal_ohlcv(target_address, "1m", limit=10)
    trend5_ok = False
    if df_5m is not None and len(df_5m) >= 5:
        try:
            df_5m['ts_dt'] = pd.to_datetime(df_5m['ts'], unit='s')
            df_5m = df_5m.set_index('ts_dt')
            close_5m = df_5m['close'].resample('5T').last().dropna()
            if len(close_5m) >= 2 and close_5m.iloc[-1] > close_5m.iloc[0]:
                trend5_ok = True
        except Exception:
            trend5_ok = False

    logger.info(f"Análise de volatilidade/tendência {pair_details['base_symbol']}: Vela {price_change_pct:+.2f}%, trend5_ok={trend5_ok}")

    if price_change_pct > 2.0 and trend5_ok:
        automation_state["checking_volatility"] = True
        automation_state["volatility_check_start_time"] = time.time()
        await send_telegram_message(f"🔔 Moeda alvo **{pair_details['base_symbol']}** selecionada. Verificando volatilidade por 3 minutos antes de entrar.")
        # Wait and watch for 3 minutes (non-blocking)
        return

# ---------------- Loop autônomo completo ----------------
async def autonomous_loop():
    global bot_running, in_position, entry_price
    logger.info("Loop autônomo iniciado.")
    while bot_running:
        try:
            now = time.time()
            force_rescan = False

            # Timeout de seleção (15 min) -> penaliza e busca novo alvo
            if not in_position and automation_state.get("current_target_pair_address") and (now - automation_state.get("target_selected_timestamp", 0) > 900):
                penalized_symbol = automation_state["current_target_symbol"]
                penalized_address = automation_state["current_target_pair_address"]
                logger.warning(f"TIMEOUT DE CAÇA: 15 min sem entrada para {penalized_symbol}. Abandonando e penalizando.")
                await send_telegram_message(f"⌛️ Timeout de caça para **{penalized_symbol}**. Procurando um novo alvo...")
                automation_state["penalty_box"][penalized_address] = 10
                automation_state["current_target_pair_address"] = None
                automation_state["checking_volatility"] = False
                automation_state["volatility_check_start_time"] = 0
                automation_state["volatility_check_passed"] = False
                force_rescan = True

            # Scan full (a cada 2 horas)
            if now - automation_state.get("last_scan_timestamp", 0) > 7200:
                force_rescan = True

            if force_rescan or not automation_state.get("current_target_pair_address"):
                # decrementa penalty box
                if automation_state["penalty_box"]:
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
                        if in_position:
                            await execute_sell_order(reason=f"Trocando para {best_coin['symbol']}")
                        automation_state.update(
                            current_target_pair_address=best_coin["pair_address"],
                            current_target_symbol=best_coin["symbol"],
                            current_target_pair_details=best_coin["details"],
                            target_selected_timestamp=now
                        )
                        automation_state["checking_volatility"] = False
                        automation_state["volatility_check_start_time"] = 0
                        automation_state["volatility_check_passed"] = False
                        await send_telegram_message(f"🎯 **Novo Alvo:** {best_coin['symbol']}. Iniciando monitoramento...")

            # Monitor / volatility check / entry logic
            if automation_state.get("current_target_pair_address") and not in_position:
                if automation_state.get("checking_volatility"):
                    pair_details = automation_state.get("current_target_pair_details")
                    data = await fetch_geckoterminal_ohlcv(pair_details['pair_address'], parameters["timeframe"], limit=1)
                    if data is not None and not data.empty:
                        last_closed_candle = data.iloc[0]
                        price_change_pct = (last_closed_candle['close'] - last_closed_candle['open']) / last_closed_candle['open'] * 100 if last_closed_candle['open'] > 0 else 0

                        # volatilidade extrema detectada -> penaliza
                        if abs(price_change_pct) > 10.0:
                            logger.warning(f"Volatilidade extrema detectada para {pair_details['base_symbol']}: {price_change_pct:.2f}%. Penalizando moeda.")
                            await send_telegram_message(f"⚠️ Volatilidade extrema (+/- 10%) detectada para **{pair_details['base_symbol']}**. A moeda será penalizada e um novo alvo será buscado.")
                            automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                            automation_state["current_target_pair_address"] = None
                            automation_state["checking_volatility"] = False
                            automation_state["volatility_check_passed"] = False
                            await asyncio.sleep(60)
                            continue

                        if now - automation_state.get("volatility_check_start_time", 0) > 180:
                            automation_state["checking_volatility"] = False
                            automation_state["volatility_check_passed"] = True
                            logger.info(f"Verificação de volatilidade de 3 minutos concluída para {pair_details['base_symbol']}. Moeda considerada segura.")
                            await send_telegram_message(f"✅ Volatilidade de **{pair_details['base_symbol']}** dentro do limite por 3 minutos. Agora, monitorando para sinal de compra (>2%).")
                    await asyncio.sleep(15)
                elif automation_state.get("volatility_check_passed"):
                    pair_details = automation_state.get("current_target_pair_details")
                    data = await fetch_geckoterminal_ohlcv(pair_details['pair_address'], parameters["timeframe"], limit=1)
                    if data is not None and not data.empty:
                        last_closed_candle = data.iloc[0]
                        price_change_pct = (last_closed_candle['close'] - last_closed_candle['open']) / last_closed_candle['open'] * 100 if last_closed_candle['open'] > 0 else 0
                        logger.info(f"Monitorando {pair_details['base_symbol']}: Variação da Vela: {price_change_pct:+.2f}%")
                        if price_change_pct > 2.0:
                            logger.info(f"Variação da vela para {pair_details['base_symbol']} > 2%. Procedendo com a compra.")
                            await send_telegram_message(f"✅ Variação da vela para **{pair_details['base_symbol']}** voltou a subir! Executando ordem de compra...")
                            price_native, _ = await fetch_dexscreener_real_time_price(pair_details['pair_address'])
                            if price_native:
                                reason = "Sinal da Estratégia retomado"
                                await execute_buy_order(parameters["amount"], price_native, pair_details, reason=reason)
                    await asyncio.sleep(15)
                else:
                    # se não em checking, tentar iniciar checagem se houver sinal
                    await check_velocity_strategy()
                    await asyncio.sleep(30)

            # Gerenciamento de posição aberta
            elif in_position:
                price_native, _ = await fetch_dexscreener_real_time_price(automation_state["current_target_pair_address"])
                if price_native:
                    profit = ((price_native - entry_price) / entry_price) * 100 if entry_price > 0 else 0
                    logger.info(f"Posição Aberta ({automation_state['current_target_symbol']}): P/L: {profit:+.2f}%")
                    tp_price = entry_price * (1 + parameters["take_profit_percent"]/100)
                    sl_price = entry_price * (1 - parameters["stop_loss_percent"]/100)

                    # Venda parcial/adaptativa
                    if profit >= parameters["take_profit_percent"]:
                        await execute_sell_order(f"Take Profit (+{parameters['take_profit_percent']}%)")
                        continue
                    if profit <= -parameters["stop_loss_percent"]:
                        await execute_sell_order(f"Stop Loss (-{parameters['stop_loss_percent']}%)")
                        continue

                    # timeout adaptativo: quanto maior o profit, maior margem de espera; se profit negativo, reduz janela
                    elapsed = time.time() - automation_state.get("position_opened_timestamp", 0)
                    # adaptive_timeout entre 60s e 1200s (20min) adaptado pelo profit
                    adaptive_timeout = max(60, min(1200, int(600 * max(0.1, (1 - profit/ (parameters['take_profit_percent'] or 1))))))
                    if elapsed > adaptive_timeout:
                        await execute_sell_order(f"Timeout adaptativo (P/L: {profit:+.2f}%)")
                        continue
                await asyncio.sleep(15)
            else:
                # sem alvo, sem posição
                await asyncio.sleep(30)

        except asyncio.CancelledError:
            logger.info("Loop autônomo cancelado.")
            break
        except Exception as e:
            logger.error(f"Erro crítico no loop autônomo: {e}", exc_info=True)
            await asyncio.sleep(30)

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
    global bot_running, periodic_task
    if parameters['amount'] is None:
        await update.effective_message.reply_text("Defina parâmetros com /set primeiro."); return
    if bot_running:
        await update.effective_message.reply_text("O bot já está em execução."); return
    bot_running = True
    logger.info("Bot de trade autônomo iniciado.")
    await update.effective_message.reply_text("🚀 Modo Caçador de Velocidade Pura iniciado!")
    if periodic_task is None or periodic_task.done():
        periodic_task = asyncio.create_task(autonomous_loop())

async def stop_bot(update, context):
    global bot_running, periodic_task, in_position
    if not bot_running:
        await update.effective_message.reply_text("O bot já está parado."); return
    bot_running = False
    if periodic_task:
        periodic_task.cancel()
    if in_position:
        await execute_sell_order("Parada manual do bot")
    automation_state.update(current_target_pair_address=None, current_target_symbol=None, last_scan_timestamp=0, position_opened_timestamp=0, target_selected_timestamp=0, penalty_box={}, checking_volatility=False, volatility_check_start_time=0)
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



