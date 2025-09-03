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
    payer = Keypair.from_base58_string(PRIVATE_KEY_B58)
    solana_client = Client(RPC_URL)
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

automation_state = {
    "current_target_pair_address": None,
    "current_target_symbol": None,
    "current_target_pair_details": None,
    "last_scan_timestamp": 0,
    "position_opened_timestamp": 0,
    "discovered_pairs": {}
}

parameters = {
    "timeframe": "1m",
    "amount": None,
    "stop_loss_percent": None,
    "take_profit_percent": None
}

# --- Funções de Execução de Ordem ---
async def execute_swap(input_mint_str, output_mint_str, amount, input_decimals, slippage_bps=500):
    logger.info(f"Iniciando swap de {amount} do token {input_mint_str} para {output_mint_str}")
    amount_wei = int(amount * (10**input_decimals))
    
    async with httpx.AsyncClient() as client:
        try:
            quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint_str}&outputMint={output_mint_str}&amount={amount_wei}&slippageBps={slippage_bps}&maxAccounts=64"
            quote_res = await client.get(quote_url, timeout=30.0)
            quote_res.raise_for_status()
            quote_response = quote_res.json()

            swap_payload = { "userPublicKey": str(payer.pubkey()), "quoteResponse": quote_response, "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True }
            swap_url = "https://quote-api.jup.ag/v6/swap"
            swap_res = await client.post(swap_url, json=swap_payload, timeout=30.0)
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
            await asyncio.sleep(8)
            solana_client.confirm_transaction(tx_signature, commitment="confirmed")
            logger.info(f"Transação confirmada: https://solscan.io/tx/{tx_signature}")
            return str(tx_signature)
        except Exception as e:
            logger.error(f"Falha na transação: {e}"); await send_telegram_message(f"⚠️ Falha na transação: {e}"); return None

async def execute_buy_order(amount, price, pair_details):
    global in_position, entry_price
    if in_position: return
    
    logger.info(f"EXECUTANDO ORDEM DE COMPRA de {amount} SOL para {pair_details['base_symbol']} ao preço de {price}")
    tx_sig = await execute_swap(pair_details['quote_address'], pair_details['base_address'], amount, 9)
    if tx_sig:
        in_position = True
        entry_price = price
        automation_state["position_opened_timestamp"] = time.time()
        log_message = (f"✅ COMPRA REALIZADA: {amount} SOL para {pair_details['base_symbol']}\n"
                       f"Entrada: {price:.10f} | Alvo: {price * (1 + parameters['take_profit_percent']/100):.10f} | "
                       f"Stop: {price * (1 - parameters['stop_loss_percent']/100):.10f}\n"
                       f"https://solscan.io/tx/{tx_sig}")
        logger.info(log_message)
        await send_telegram_message(log_message)
    else:
        await send_telegram_message(f"❌ FALHA NA COMPRA do token {pair_details['base_symbol']}")

async def execute_sell_order(reason=""):
    global in_position, entry_price
    if not in_position: return
    
    pair_details = automation_state.get('current_target_pair_details', {})
    symbol = pair_details.get('base_symbol', 'TOKEN')
    logger.info(f"EXECUTANDO ORDEM DE VENDA de {symbol}. Motivo: {reason}")
    try:
        token_mint_pubkey = Pubkey.from_string(pair_details['base_address'])
        ata_address = get_associated_token_address(payer.pubkey(), token_mint_pubkey)
        balance_response = solana_client.get_token_account_balance(ata_address)
        token_balance_data = balance_response.value
        amount_to_sell = token_balance_data.ui_amount
        if amount_to_sell is None or amount_to_sell == 0:
            logger.warning("Tentativa de venda com saldo zero."); in_position = False; entry_price = 0.0; return

        tx_sig = await execute_swap(pair_details['base_address'], pair_details['quote_address'], amount_to_sell, token_balance_data.decimals)
        if tx_sig:
            log_message = f"🛑 VENDA REALIZADA: {symbol}\nMotivo: {reason}\nhttps://solscan.io/tx/{tx_sig}"
            logger.info(log_message)
            await send_telegram_message(log_message)
        else:
            await send_telegram_message(f"❌ FALHA NA VENDA do token {symbol}")
    except Exception as e:
        logger.error(f"Erro ao vender {symbol}: {e}"); await send_telegram_message(f"⚠️ Falha ao vender {symbol}: {e}")
    finally:
        in_position = False
        entry_price = 0.0
        automation_state["position_opened_timestamp"] = 0

# --- Funções de Análise e Descoberta ---
async def fetch_geckoterminal_ohlcv(pair_address, timeframe, limit=60):
    timeframe_map = {"1m": "minute", "5m": "minute"}
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
            return {"base_symbol": pair_data['baseToken']['symbol'], "quote_symbol": pair_data['quoteToken']['symbol'], "base_address": pair_data['baseToken']['address'], "quote_address": pair_data['quoteToken']['address']}
    except Exception: return None

# --- FUNÇÃO DE DESCOBERTA COM LOGS DETALHADOS ---
async def discover_and_filter_pairs():
    logger.info("--- FASE 1: DESCOBERTA --- Buscando os top 100 pares no GeckoTerminal...")
    all_pools = []
    
    for page in range(1, 6):
        url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools?page={page}&include=base_token,quote_token"
        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(url, timeout=20.0)
                res.raise_for_status()
                pools_data = res.json().get('data', [])
                if not pools_data: break
                all_pools.extend(pools_data)
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

            logger.info(f"Analisando candidato: {symbol}...")
            
            # Filtros
            liquidity = float(attr.get('reserve_in_usd', 0))
            if liquidity < 200000:
                rejection_reasons.append(f"Liquidez Baixa (${liquidity:,.0f})")

            volume_24h = float(attr.get('volume_usd', {}).get('h24', 0))
            if volume_24h < 1000000:
                rejection_reasons.append(f"Volume 24h Baixo (${volume_24h:,.0f})")

            age_str = attr.get('pool_created_at')
            if age_str:
                age_dt = datetime.fromisoformat(age_str.replace('Z', '+00:00'))
                age_hours = (datetime.now(timezone.utc) - age_dt).total_seconds() / 3600
                if age_hours < 0.5:
                    rejection_reasons.append(f"Muito Nova ({age_hours:.2f} horas)")
            
            quote_token_addr = relationships.get('quote_token', {}).get('data', {}).get('id')
            if quote_token_addr != 'So11111111111111111111111111111111111111112':
                 rejection_reasons.append("Não é par contra SOL")

            # Decisão
            if not rejection_reasons:
                logger.info(f"✅ APROVADO: {symbol} | Liquidez: ${liquidity:,.0f}, Volume: ${volume_24h:,.0f}")
                filtered_pairs[symbol] = address
            else:
                logger.info(f"❌ DESCARTADO: {symbol} | Motivos: {', '.join(rejection_reasons)}")
                
        except (ValueError, TypeError, KeyError, IndexError):
            continue

    logger.info(f"Descoberta finalizada. {len(filtered_pairs)} pares passaram nos filtros.")
    return filtered_pairs

async def analyze_and_score_coin(pair_address, symbol):
    try:
        df = await fetch_geckoterminal_ohlcv(pair_address, "1m", limit=60)
        if df is None or len(df) < 30: return 0, None
        
        price_range = df['high'].max() - df['low'].min()
        volatility_score = (price_range / df['low'].min()) * 100
        volume_score = df['volume'].sum()
        
        final_score = (volatility_score * 1000) + volume_score
        pair_details = await get_pair_details(pair_address)
        return final_score, pair_details
    except Exception as e:
        logger.error(f"Erro ao analisar {symbol} ({pair_address}): {e}"); return 0, None

async def find_best_coin_to_trade(candidate_pairs):
    logger.info("--- FASE 2: SELEÇÃO --- Pontuando os melhores pares...")
    if not candidate_pairs:
        logger.warning("Nenhum candidato para pontuar.")
        return None
        
    best_score, best_coin_info = -1, None
    tasks = [analyze_and_score_coin(addr, symbol) for symbol, addr in candidate_pairs.items()]
    results = await asyncio.gather(*tasks)
    for (symbol, addr), (score, details) in zip(candidate_pairs.items(), results):
        if details and score > 0:
            logger.info(f"Candidato: {symbol} | Pontuação: {score:.2f}")
            if score > best_score:
                best_score = score
                best_coin_info = {"symbol": symbol, "pair_address": addr, "score": score, "details": details}
    if best_coin_info:
        logger.info(f"--- SELEÇÃO FINALIZADA --- Melhor moeda: {best_coin_info['symbol']} (Pontuação: {best_coin_info['score']:.2f})")
    else:
        logger.warning("--- SELEÇÃO FINALIZADA --- Nenhuma moeda com oportunidade clara encontrada.")
    return best_coin_info

async def check_scalping_strategy():
    global in_position, entry_price
    target_address = automation_state.get("current_target_pair_address")
    if not target_address:
        logger.info("Nenhuma moeda alvo definida. Aguardando próximo scan.")
        return
    pair_details = automation_state.get("current_target_pair_details")
    if not in_position:
        data = await fetch_geckoterminal_ohlcv(target_address, parameters["timeframe"])
        if data is None or len(data) < 20: return
        data.ta.ema(length=5, append=True, col_names=('EMA_5',))
        data.ta.ema(length=10, append=True, col_names=('EMA_10',))
        data['VOL_MA_9'] = data['volume'].rolling(window=9).mean()
        data.dropna(inplace=True);
        if len(data) < 5: return
        last = data.iloc[-1]
        is_bullish_state = last['EMA_5'] > last['EMA_10']
        volume_is_high = last['volume'] > last['VOL_MA_9']
        if is_bullish_state and volume_is_high:
            crossover_in_window = False
            for i in range(1, 4):
                if len(data) > i + 1 and (data.iloc[-i]['EMA_5'] > data.iloc[-i]['EMA_10'] and data.iloc[-i-1]['EMA_5'] <= data.iloc[-i-1]['EMA_10']):
                    crossover_in_window = True; break
            if crossover_in_window:
                price, _ = await fetch_dexscreener_real_time_price(target_address)
                if price:
                    reason = f"Cruzamento de EMA (últimas 3 velas) com Volume ({last['volume']:.2f}) > Média ({last['VOL_MA_9']:.2f})"
                    await execute_buy_order(parameters["amount"], price, pair_details)

async def autonomous_loop():
    global bot_running
    logger.info("Loop de caça autônoma iniciado.")
    while bot_running:
        try:
            now = time.time()
            if now - automation_state.get("last_scan_timestamp", 0) > 7200:
                discovered_pairs = await discover_and_filter_pairs()
                automation_state["discovered_pairs"] = discovered_pairs
                best_coin = await find_best_coin_to_trade(discovered_pairs)
                automation_state["last_scan_timestamp"] = now
                if best_coin and best_coin["pair_address"] != automation_state.get("current_target_pair_address"):
                    logger.info(f"Novo alvo com maior potencial encontrado: {best_coin['symbol']}.")
                    if in_position:
                        await execute_sell_order(reason=f"Trocando para {best_coin['symbol']}")
                    automation_state.update(current_target_pair_address=best_coin["pair_address"], current_target_symbol=best_coin["symbol"], current_target_pair_details=best_coin["details"])
                    await send_telegram_message(f"🎯 **Novo Alvo:** {best_coin['symbol']}. Procurando por entradas...")

            if not automation_state.get("current_target_pair_address"):
                if not automation_state.get("discovered_pairs"):
                    automation_state["discovered_pairs"] = await discover_and_filter_pairs()
                best_coin = await find_best_coin_to_trade(automation_state["discovered_pairs"])
                automation_state["last_scan_timestamp"] = now
                if best_coin:
                    automation_state.update(current_target_pair_address=best_coin["pair_address"], current_target_symbol=best_coin["symbol"], current_target_pair_details=best_coin["details"])
                    await send_telegram_message(f"🎯 **Alvo Definido:** {best_coin['symbol']}. Iniciando operações.")
            
            if not in_position:
                await check_scalping_strategy()
                await asyncio.sleep(30)
            else:
                price, _ = await fetch_dexscreener_real_time_price(automation_state["current_target_pair_address"])
                if price:
                    take_profit_price = entry_price * (1 + parameters["take_profit_percent"] / 100)
                    stop_loss_price = entry_price * (1 - parameters["stop_loss_percent"] / 100)
                    if price >= take_profit_price: await execute_sell_order(f"Take Profit (+{parameters['take_profit_percent']}%)"); continue
                    if price <= stop_loss_price: await execute_sell_order(f"Stop Loss (-{parameters['stop_loss_percent']}%)"); continue
                    if time.time() - automation_state.get("position_opened_timestamp", 0) > 1800:
                        await execute_sell_order("Timeout de 30 minutos"); continue
                await asyncio.sleep(15)
        except asyncio.CancelledError:
            logger.info("Loop autônomo cancelado."); break
        except Exception as e:
            logger.error(f"Erro crítico no loop autônomo: {e}", exc_info=True); await asyncio.sleep(60)

# --- Comandos do Telegram ---
async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Sou seu bot **v14.3 (Log de Filtragem)**.\n\n'
        '**Dinâmica Autônoma:**\n'
        'Eu escaneio os TOP 100 pares, aplico filtros de segurança e pontuo os melhores para operar, trocando de alvo a cada 2 horas.\n\n'
        '**Gerenciamento de Risco:**\n'
        'Posições abertas por mais de 30 minutos são fechadas automaticamente.\n\n'
        '**Configure-me uma vez com `/set` e depois use `/run`.**\n'
        '`/set <VALOR> <STOP_LOSS_%> <TAKE_PROFIT_%>`\n'
        '**Ex:** `/set 0.1 1.0 1.5`',
        parse_mode='Markdown'
    )

async def set_params(update, context):
    if bot_running:
        await update.effective_message.reply_text("Pare o bot com /stop antes de alterar os parâmetros."); return
    try:
        amount, stop_loss, take_profit = float(context.args[0]), float(context.args[1]), float(context.args[2])
        if stop_loss <= 0 or take_profit <= 0:
            await update.effective_message.reply_text("⚠️ Stop Loss e Take Profit devem ser valores positivos."); return
        parameters.update(amount=amount, stop_loss_percent=stop_loss, take_profit_percent=take_profit)
        await update.effective_message.reply_text(
            f"✅ *Parâmetros de Scalping definidos!*\n"
            f"💰 *Valor por Ordem:* `{amount}` SOL\n"
            f"🛑 *Stop Loss:* `-{stop_loss}%`\n"
            f"🎯 *Take Profit:* `+{take_profit}%`\n\n"
            "Agora use `/run` para iniciar.",
            parse_mode='Markdown'
        )
    except (IndexError, ValueError):
        await update.effective_message.reply_text("⚠️ *Formato incorreto.*\nUse: `/set <VALOR> <STOP_LOSS_%> <TAKE_PROFIT_%>`\nEx: `/set 0.1 1.0 1.5`", parse_mode='Markdown')

async def run_bot(update, context):
    global bot_running, periodic_task
    if not all(p is not None for p in parameters.values()):
        await update.effective_message.reply_text("Defina os parâmetros com /set primeiro."); return
    if bot_running:
        await update.effective_message.reply_text("O bot já está em execução."); return
    bot_running = True
    logger.info("Bot de trade autônomo iniciado.")
    await update.effective_message.reply_text("🚀 Modo de caça autônoma iniciado!")
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
    automation_state.update(current_target_pair_address=None, current_target_symbol=None, last_scan_timestamp=0, position_opened_timestamp=0)
    logger.info("Bot de trade parado.")
    await update.effective_message.reply_text("🛑 Bot parado. Todas as tarefas e posições foram finalizadas.")

async def send_telegram_message(message):
    if application:
        await application.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')

def main():
    global application
    keep_alive()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set", set_params))
    application.add_handler(CommandHandler("run", run_bot))
    application.add_handler(CommandHandler("stop", stop_bot))
    logger.info("Bot do Telegram iniciado e aguardando comandos...")
    application.run_polling()

if __name__ == '__main__':
    main()
