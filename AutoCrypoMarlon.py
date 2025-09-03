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
    "target_selected_timestamp": 0,
    "penalty_box": {}, # DE: set() PARA: dict() para contar as rodadas
    "discovered_pairs": {}
}

parameters = {
    "timeframe": "1m",
    "amount": None,
    "stop_loss_percent": None,
    "take_profit_percent": None,
    "volume_multiplier": None # NOVO PARÂMETRO
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
        automation_state["current_target_pair_address"] = None # Força re-scan imediato

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
            
            is_sol_pair = False
            quote_token_addr = relationships.get('quote_token', {}).get('data', {}).get('id')
            if quote_token_addr == 'So11111111111111111111111111111111111111112' or attr.get('name', '').endswith(' / SOL'):
                is_sol_pair = True

            if not is_sol_pair: rejection_reasons.append("Não é par contra SOL")
            
            liquidity = float(attr.get('reserve_in_usd', 0))
            if liquidity < 200000: rejection_reasons.append(f"Liquidez Baixa (${liquidity:,.0f})")

            volume_24h = float(attr.get('volume_usd', {}).get('h24', 0))
            if volume_24h < 1000000: rejection_reasons.append(f"Volume 24h Baixo (${volume_24h:,.0f})")

            age_str = attr.get('pool_created_at')
            if age_str:
                age_dt = datetime.fromisoformat(age_str.replace('Z', '+00:00'))
                age_hours = (datetime.now(timezone.utc) - age_dt).total_seconds() / 3600
                if age_hours < 2.0:
                    rejection_reasons.append(f"Muito Nova ({age_hours:.2f} horas)")
            
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

async def find_best_coin_to_trade(candidate_pairs, penalized_pairs=set()):
    logger.info("--- FASE 2: SELEÇÃO --- Pontuando os melhores pares...")
    if not candidate_pairs:
        logger.warning("Nenhum candidato para pontuar."); return None
        
    best_score, best_coin_info = -1, None
    
    # Filtra os candidatos antes de criar as tasks
    valid_candidates = {s: a for s, a in candidate_pairs.items() if a not in penalized_pairs}
    if not valid_candidates:
        logger.warning("Nenhum candidato válido após remover os penalizados.")
        return None

    tasks = [analyze_and_score_coin(addr, symbol) for symbol, addr in valid_candidates.items()]
    results = await asyncio.gather(*tasks)

    for (symbol, addr), (score, details) in zip(valid_candidates.items(), results):
        if details and score > 0:
            logger.info(f"Candidato: {symbol} | Pontuação: {score:.2f}")
            if score > best_score:
                best_score, best_coin_info = score, {"symbol": symbol, "pair_address": addr, "score": score, "details": details}
    if best_coin_info:
        logger.info(f"--- SELEÇÃO FINALIZADA --- Melhor moeda: {best_coin_info['symbol']} (Pontuação: {best_coin_info['score']:.2f})")
    else:
        logger.warning("--- SELEÇÃO FINALIZADA --- Nenhuma moeda com oportunidade clara encontrada.")
    return best_coin_info

# --- ESTRATÉGIA ---
async def check_breakout_strategy():
    global in_position, entry_price
    target_address = automation_state.get("current_target_pair_address")
    if not target_address or in_position: return

    pair_details = automation_state.get("current_target_pair_details")
    data = await fetch_geckoterminal_ohlcv(target_address, parameters["timeframe"], limit=30)
    if data is None or len(data) < 20: return

    current_candle = data.iloc[-1]
    lookback_period = 15
    analysis_window = data.iloc[-(lookback_period+1):-1]
    if analysis_window.empty: return

    resistance = analysis_window['high'].max()
    volume_ma = analysis_window['volume'].mean()
    volume_breakout_threshold = volume_ma * parameters["volume_multiplier"]

    logger.info(f"Análise Compra ({pair_details['base_symbol']}): "
                f"Preço Atual={current_candle['close']:.10f} | Resistência={resistance:.10f} | "
                f"Volume={current_candle['volume']:.2f} | Vol. Necessário={volume_breakout_threshold:.2f}")

    price_breakout = current_candle['close'] > resistance
    volume_confirmed = current_candle['volume'] > volume_breakout_threshold

    if price_breakout and volume_confirmed:
        price, _ = await fetch_dexscreener_real_time_price(target_address)
        if price:
            reason = f"Rompimento da resistência {resistance:.10f} com volume explosivo ({current_candle['volume']:.2f})."
            await execute_buy_order(parameters["amount"], price, pair_details)

# --- Loop Principal Autônomo ---
async def autonomous_loop():
    global bot_running
    logger.info("Loop de caça autônoma iniciado.")
    while bot_running:
        try:
            now = time.time()
            force_rescan = False
            
            # Timeout de Caça (15 minutos)
            if not in_position and automation_state.get("current_target_pair_address") and (now - automation_state.get("target_selected_timestamp", 0) > 900):
                penalized_symbol = automation_state["current_target_symbol"]
                penalized_address = automation_state["current_target_pair_address"]
                logger.warning(f"TIMEOUT DE CAÇA: 15 min sem entrada para {penalized_symbol}. Abandonando e penalizando.")
                await send_telegram_message(f"⌛️ Timeout de caça para **{penalized_symbol}**. Procurando um novo alvo...")
                automation_state["penalty_box"][penalized_address] = 10 # Adiciona/reseta a contagem para 10
                automation_state["current_target_pair_address"] = None
                force_rescan = True

            # Re-scan a cada 2 horas
            if now - automation_state.get("last_scan_timestamp", 0) > 7200:
                logger.info("Timer de 2 horas atingido. Iniciando novo ciclo de descoberta.")
                force_rescan = True
            
            # Ciclo de Descoberta/Seleção
            if force_rescan or not automation_state.get("current_target_pair_address"):
                
                # Gerencia a caixa de penalidade
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
                        await send_telegram_message(f"🎯 **Novo Alvo:** {best_coin['symbol']}. Iniciando monitoramento...")
            
            # Executa a estratégia de trading
            if not in_position and automation_state.get("current_target_pair_address"):
                await check_breakout_strategy()
                await asyncio.sleep(30)
            elif in_position:
                price, _ = await fetch_dexscreener_real_time_price(automation_state["current_target_pair_address"])
                if price:
                    profit = ((price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
                    logger.info(f"Posição Aberta ({automation_state['current_target_symbol']}): P/L: {profit:+.2f}%")
                    take_profit_price = entry_price * (1 + parameters["take_profit_percent"] / 100)
                    stop_loss_price = entry_price * (1 - parameters["stop_loss_percent"] / 100)
                    
                    if price >= take_profit_price: await execute_sell_order(f"Take Profit (+{parameters['take_profit_percent']}%)"); continue
                    if price <= stop_loss_price: await execute_sell_order(f"Stop Loss (-{parameters['stop_loss_percent']}%)"); continue
                    if time.time() - automation_state.get("position_opened_timestamp", 0) > 1800: # Timeout de posição de 30min
                        await execute_sell_order("Timeout de 30 minutos"); continue
                await asyncio.sleep(15)
            else:
                await asyncio.sleep(60)

        except asyncio.CancelledError:
            logger.info("Loop autônomo cancelado."); break
        except Exception as e:
            logger.error(f"Erro crítico no loop autônomo: {e}", exc_info=True); await asyncio.sleep(60)

# --- Comandos do Telegram ---
async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Sou seu bot **v16.0 (Caçador Adaptável)**.\n\n'
        '**Dinâmica Autônoma:**\n'
        '1. Eu descubro e seleciono a melhor moeda para operar.\n'
        '2. Se eu não encontrar uma entrada em **15 minutos**, abandono o alvo e o penalizo por 10 buscas.\n'
        '3. Após fechar qualquer operação, eu imediatamente procuro uma nova oportunidade.\n\n'
        '**Estratégia:** Breakout com confirmação de volume explosivo (multiplicador ajustável).\n\n'
        '**NOVO COMANDO `/set`:**\n'
        '`/set <VALOR> <STOP_LOSS_%> <TAKE_PROFIT_%> <VOL_MULT>`\n'
        '**Ex:** `/set 0.1 1.5 2.5 3.0`',
        parse_mode='Markdown'
    )

async def set_params(update, context):
    if bot_running:
        await update.effective_message.reply_text("Pare o bot com /stop antes de alterar os parâmetros."); return
    try:
        amount, stop_loss, take_profit, vol_mult = float(context.args[0]), float(context.args[1]), float(context.args[2]), float(context.args[3])
        if stop_loss <= 0 or take_profit <= 0 or vol_mult <= 1.0:
            await update.effective_message.reply_text("⚠️ Stop/Profit devem ser > 0. Multiplicador de Volume deve ser > 1.0."); return
        parameters.update(amount=amount, stop_loss_percent=stop_loss, take_profit_percent=take_profit, volume_multiplier=vol_mult)
        await update.effective_message.reply_text(
            f"✅ *Parâmetros de Scalping definidos!*\n"
            f"💰 *Valor por Ordem:* `{amount}` SOL\n"
            f"🛑 *Stop Loss:* `-{stop_loss}%`\n"
            f"🎯 *Take Profit:* `+{take_profit}%`\n"
            f"🔊 *Mult. de Volume:* `{vol_mult}x`\n\n"
            "Agora use `/run` para iniciar.",
            parse_mode='Markdown'
        )
    except (IndexError, ValueError):
        await update.effective_message.reply_text("⚠️ *Formato incorreto.*\nUse: `/set <VALOR> <STOP> <PROFIT> <VOL_MULT>`\nEx: `/set 0.1 1.5 2.5 3.0`", parse_mode='Markdown')

async def run_bot(update, context):
    global bot_running, periodic_task
    if not all(p is not None for p in parameters.values()):
        await update.effective_message.reply_text("Defina os parâmetros com /set primeiro."); return
    if bot_running:
        await update.effective_message.reply_text("O bot já está em execução."); return
    bot_running = True
    logger.info("Bot de trade autônomo iniciado.")
    await update.effective_message.reply_text("🚀 Modo de caça autônoma (Breakout) iniciado!")
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
    automation_state.update(current_target_pair_address=None, current_target_symbol=None, last_scan_timestamp=0, position_opened_timestamp=0, target_selected_timestamp=0, penalty_box={})
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
