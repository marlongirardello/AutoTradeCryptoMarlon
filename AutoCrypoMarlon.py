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

# --- Configuração do Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# --- Cliente Solana e Carteira ---
try:
    payer = Keypair.from_base58_string(PRIVATE_KEY_B58)
    solana_client = Client(RPC_URL)
    logger.info(f"Carteira carregada com sucesso. Endereço público: {payer.pubkey()}")
except Exception as e:
    logger.error(f"Erro ao carregar a carteira Solana: {e}")
    exit()

# --- LISTA DE MOEDAS PARA O SCANNER ---
# Adicione ou remova moedas aqui. Use o "Pair Address" do Dexscreener.
CANDIDATE_TOKENS = {
    "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", 
    "TROLL": "0xf8ebf4849f1fa4faf0dff2106a173d3a6cb2eb3a", 
    "PENGU": "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv",
    "PUMP": "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn",
    "MROCKS": "moon3CP11XLvrAxUPBnPtueDEJvmjqAyZwPuq7wBC1y",
    "PWEASE": "CniPCE4b3s8gSUPhUiyMjXnytrEqUrMfSsnbBjLCpump",
    "Clippy": "7eMJmn1bYWSQEwxAX7CyngBzGNGu1cT582asKxxRpump",
    "CLANKER": "3qq54YqAKG3TcrwNHXFSpMCWoL8gmMuPceJ4FG9npump",
    "SPARK": "5zCETicUCJqJ5Z3wbfFPZqtSpHPYqnggs1wX7ZRpump",
    "TOBAKU": "H8xQ6poBjB9DTPMDTKWzWPrnxu4bDEhybxiouF8Ppump",
    "USELESS": "Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk",
}


# --- Variáveis Globais de Estado ---
bot_running = False
in_position = False
entry_price = 0.0
periodic_task = None
application = None

# Dicionário para gerenciar o estado da automação
automation_state = {
    "current_target_pair_address": None,
    "current_target_symbol": None,
    "last_scan_timestamp": 0,
    "position_opened_timestamp": 0
}

# Parâmetros da estratégia
parameters = {
    "timeframe": "1m",
    "amount": None,
    "stop_loss_percent": None,
    "take_profit_percent": None
}

# --- Funções de Execução de Ordem ---
async def execute_swap(input_mint_str, output_mint_str, amount, input_decimals, slippage_bps=500):
    # ... (código do execute_swap permanece o mesmo) ...
    pass

async def execute_buy_order(amount, price, pair_details):
    global in_position, entry_price
    if in_position: return
    
    logger.info(f"EXECUTANDO ORDEM DE COMPRA de {amount} SOL para {pair_details['base_symbol']} ao preço de {price}")
    # ... (lógica de swap) ...
    # SIMULAÇÃO DE SUCESSO PARA TESTE
    in_position = True
    entry_price = price
    automation_state["position_opened_timestamp"] = time.time()
    await send_telegram_message(f"✅ COMPRA REALIZADA: {amount} SOL para {pair_details['base_symbol']}\n"
                              f"Entrada: {price:.10f} | Alvo: {price * (1 + parameters['take_profit_percent']/100):.10f} | "
                              f"Stop: {price * (1 - parameters['stop_loss_percent']/100):.10f}")

async def execute_sell_order(reason=""):
    global in_position, entry_price
    if not in_position: return
    
    pair_details = automation_state.get('current_target_pair_details', {})
    symbol = pair_details.get('base_symbol', 'TOKEN')
    logger.info(f"EXECUTANDO ORDEM DE VENDA de {symbol}. Motivo: {reason}")
    # ... (lógica de swap para vender todo o saldo) ...
    # SIMULAÇÃO DE SUCESSO PARA TESTE
    in_position = False
    entry_price = 0.0
    automation_state["position_opened_timestamp"] = 0
    await send_telegram_message(f"🛑 VENDA REALIZADA: {symbol}\nMotivo: {reason}")


# --- Funções de Análise e Decisão ---
async def get_pair_details(pair_address):
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            res.raise_for_status()
            pair_data = res.json().get('pair')
            if not pair_data: return None
            
            return {
                "base_symbol": pair_data['baseToken']['symbol'],
                "quote_symbol": pair_data['quoteToken']['symbol'],
                "base_address": pair_data['baseToken']['address'],
                "quote_address": pair_data['quoteToken']['address'],
            }
    except Exception:
        return None

async def analyze_and_score_coin(pair_address):
    """Analisa uma moeda e retorna uma pontuação de oportunidade."""
    try:
        # Usaremos o GeckoTerminal para obter um histórico mais rico para análise
        url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair_address}/ohlcv/minute?aggregate=1&limit=60"
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=10.0)
            response.raise_for_status()
            api_data = response.json()
            
            if not (api_data.get('data') and api_data['data'].get('attributes', {}).get('ohlcv_list')):
                return 0, None # Retorna 0 se não houver dados

            ohlcv_list = api_data['data']['attributes']['ohlcv_list']
            if len(ohlcv_list) < 30: return 0, None # Precisa de um histórico mínimo

            df = pd.DataFrame(ohlcv_list, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume_usd'])
            df[['open', 'high', 'low', 'close', 'volume_usd']] = df[['open', 'high', 'low', 'close', 'volume_usd']].apply(pd.to_numeric)

            # Métrica 1: Volatilidade (range percentual da última hora)
            price_range = df['high'].max() - df['low'].min()
            volatility_score = (price_range / df['low'].min()) * 100

            # Métrica 2: Volume (soma do volume em USD na última hora)
            volume_score = df['volume_usd'].sum()
            
            # Pontuação Final (ponderada para dar mais importância ao volume)
            final_score = (volatility_score * 1000) + volume_score
            
            pair_details = await get_pair_details(pair_address)
            return final_score, pair_details
    except Exception as e:
        logger.error(f"Erro ao analisar {pair_address}: {e}")
        return 0, None


async def find_best_coin_to_trade():
    """Escaneia a lista de candidatos e retorna o melhor para operar."""
    logger.info("--- INICIANDO SCANNER DE MELHORES MOEDAS ---")
    best_score = -1
    best_coin_info = None

    tasks = [analyze_and_score_coin(addr) for symbol, addr in CANDIDATE_TOKENS.items()]
    results = await asyncio.gather(*tasks)

    for (symbol, addr), (score, details) in zip(CANDIDATE_TOKENS.items(), results):
        if details:
            logger.info(f"Moeda: {symbol} | Pontuação: {score:.2f}")
            if score > best_score:
                best_score = score
                best_coin_info = {
                    "symbol": symbol,
                    "pair_address": addr,
                    "score": score,
                    "details": details
                }
    
    if best_coin_info:
        logger.info(f"--- SCANNER FINALIZADO --- Melhor moeda encontrada: {best_coin_info['symbol']} com pontuação {best_coin_info['score']:.2f}")
    else:
        logger.warning("--- SCANNER FINALIZADO --- Nenhuma moeda viável encontrada.")
    
    return best_coin_info


async def check_scalping_strategy():
    """Executa a estratégia de scalping para a moeda alvo atual."""
    global in_position, entry_price
    
    target_address = automation_state.get("current_target_pair_address")
    if not target_address:
        logger.warning("Nenhuma moeda alvo definida para operar.")
        return

    pair_details = automation_state.get("current_target_pair_details")
    current_price_native, _ = await fetch_dexscreener_real_time_price(target_address)
    if current_price_native is None: return

    # Lógica de Timeout e Saída
    if in_position:
        position_duration = time.time() - automation_state["position_opened_timestamp"]
        
        if position_duration > 1800: # 30 minutos
            await execute_sell_order(reason=f"Timeout de 30 minutos atingido.")
            automation_state["current_target_pair_address"] = None # Força novo scan
            return

        take_profit_price = entry_price * (1 + parameters["take_profit_percent"] / 100)
        stop_loss_price = entry_price * (1 - parameters["stop_loss_percent"] / 100)
        
        if current_price_native >= take_profit_price:
            await execute_sell_order(reason=f"Take Profit (+{parameters['take_profit_percent']}%) atingido")
        elif current_price_native <= stop_loss_price:
            await execute_sell_order(reason=f"Stop Loss (-{parameters['stop_loss_percent']}%) atingido")
        return

    # Lógica de Compra
    data = await fetch_geckoterminal_ohlcv(target_address, parameters["timeframe"])
    if data is None or len(data) < 20: return

    data.ta.ema(length=5, append=True, col_names=('EMA_5',))
    data.ta.ema(length=10, append=True, col_names=('EMA_10',))
    data['VOL_MA_9'] = data['volume'].rolling(window=9).mean()
    data.dropna(inplace=True)
    if len(data) < 5: return

    last = data.iloc[-1]
    
    is_bullish_state = last['EMA_5'] > last['EMA_10']
    volume_is_high = last['volume'] > last['VOL_MA_9']
    
    if is_bullish_state and volume_is_high:
        crossover_in_window = False
        for i in range(1, 4):
            if len(data) > i + 1:
                if data.iloc[-i]['EMA_5'] > data.iloc[-i]['EMA_10'] and data.iloc[-i-1]['EMA_5'] <= data.iloc[-i-1]['EMA_10']:
                    crossover_in_window = True; break
        
        if crossover_in_window:
            reason = f"Cruzamento de EMA (últimas 3 velas) com Volume ({last['volume']:.2f}) > Média ({last['VOL_MA_9']:.2f})"
            await execute_buy_order(parameters["amount"], current_price_native, pair_details)

# --- Loop Principal Autônomo ---
async def autonomous_loop():
    global bot_running
    logger.info("Loop autônomo iniciado.")
    
    while bot_running:
        try:
            now = time.time()
            
            # 1. Lógica de re-scan a cada 2 horas
            if now - automation_state["last_scan_timestamp"] > 7200: # 2 horas
                logger.info("Timer de 2 horas atingido. Buscando a melhor moeda para operar...")
                best_coin = await find_best_coin_to_trade()
                automation_state["last_scan_timestamp"] = now

                if best_coin and best_coin["pair_address"] != automation_state["current_target_pair_address"]:
                    logger.info(f"Nova moeda encontrada: {best_coin['symbol']}. Trocando de alvo.")
                    if in_position:
                        await execute_sell_order(reason=f"Trocando para moeda com maior potencial: {best_coin['symbol']}")
                    
                    automation_state["current_target_pair_address"] = best_coin["pair_address"]
                    automation_state["current_target_symbol"] = best_coin["symbol"]
                    automation_state["current_target_pair_details"] = best_coin["details"]
                    await send_telegram_message(f"🎯 Novo alvo: **{best_coin['symbol']}**. Procurando por entradas...")

            # 2. Se não tem alvo, tenta encontrar um
            if not automation_state.get("current_target_pair_address"):
                logger.info("Nenhuma moeda alvo definida. Realizando scan inicial...")
                best_coin = await find_best_coin_to_trade()
                automation_state["last_scan_timestamp"] = now
                if best_coin:
                    automation_state["current_target_pair_address"] = best_coin["pair_address"]
                    automation_state["current_target_symbol"] = best_coin["symbol"]
                    automation_state["current_target_pair_details"] = best_coin["details"]
                    await send_telegram_message(f"🎯 Alvo definido: **{best_coin['symbol']}**. Iniciando operações.")
            
            # 3. Executa a estratégia de scalping no alvo atual
            if not in_position:
                await check_scalping_strategy()
            else: # Se em posição, verifica mais rápido
                await asyncio.sleep(15) # Verifica SL/TP a cada 15s
                continue

            await asyncio.sleep(30) # Se não em posição, verifica a cada 30s
            
        except asyncio.CancelledError:
            logger.info("Loop autônomo cancelado."); break
        except Exception as e:
            logger.error(f"Erro crítico no loop autônomo: {e}", exc_info=True)
            await asyncio.sleep(60)

# --- Comandos do Telegram ---
async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Sou seu bot de **Trading Autônomo v12.0 (Hunter Mode)**.\n\n'
        '**Dinâmica:**\n'
        '1. A cada 2 horas, eu escaneio uma lista de moedas para encontrar a melhor oportunidade (maior volatilidade e volume).\n'
        '2. Eu troco de alvo automaticamente se encontrar uma moeda melhor.\n'
        '3. Se uma operação ficar aberta por mais de 30 minutos, eu a fecho e procuro uma nova oportunidade.\n\n'
        '**Estratégia:** Scalping de Momentum com cruzamento de EMA e confirmação de volume.\n\n'
        '**Use `/set` para configurar os parâmetros de trade UMA VEZ:**\n'
        '`/set <VALOR> <STOP_LOSS_%> <TAKE_PROFIT_%>`\n\n'
        '**Exemplo:**\n'
        '`/set 0.1 1.0 1.5`\n\n'
        '**Comandos:**\n'
        '`/run` - Inicia o modo de caça autônoma.\n'
        '`/stop` - Para o bot.',
        parse_mode='Markdown'
    )

async def set_params(update, context):
    if bot_running:
        await update.effective_message.reply_text("Pare o bot com /stop antes de alterar os parâmetros.")
        return
    try:
        amount, stop_loss, take_profit = float(context.args[0]), float(context.args[1]), float(context.args[2])
        if stop_loss <= 0 or take_profit <= 0:
            await update.effective_message.reply_text("⚠️ Stop Loss e Take Profit devem ser valores positivos."); return

        parameters["amount"] = amount
        parameters["stop_loss_percent"] = stop_loss
        parameters["take_profit_percent"] = take_profit

        await update.effective_message.reply_text(
            f"✅ *Parâmetros de Scalping definidos!*\n\n"
            f"💰 *Valor por Ordem:* `{amount}` SOL\n"
            f"🛑 *Stop Loss:* `-{stop_loss}%`\n"
            f"🎯 *Take Profit:* `+{take_profit}%`\n\n"
            "Agora use `/run` para iniciar o bot.",
            parse_mode='Markdown'
        )
    except (IndexError, ValueError):
        await update.effective_message.reply_text(
            "⚠️ *Formato incorreto.*\n"
            "Use: `/set <VALOR> <STOP_LOSS_%> <TAKE_PROFIT_%>`\n"
            "Exemplo: `/set 0.1 1.0 1.5`",
            parse_mode='Markdown'
        )

async def run_bot(update, context):
    global bot_running, periodic_task
    if not all(parameters.values()):
        await update.effective_message.reply_text("Defina os parâmetros com /set primeiro."); return
    if bot_running:
        await update.effective_message.reply_text("O bot já está em execução."); return
    
    bot_running = True
    logger.info("Bot de trade autônomo iniciado.")
    await update.effective_message.reply_text("🚀 Modo de caça autônoma iniciado! O bot agora vai procurar e operar as melhores moedas por conta própria.")
    
    if periodic_task is None or periodic_task.done():
        periodic_task = asyncio.create_task(autonomous_loop())

async def stop_bot(update, context):
    global bot_running, periodic_task, in_position, entry_price
    if not bot_running:
        await update.effective_message.reply_text("O bot já está parado."); return
    
    bot_running = False
    if periodic_task:
        periodic_task.cancel()
        periodic_task = None

    if in_position:
        await execute_sell_order("Parada manual do bot")

    in_position, entry_price = False, 0.0
    automation_state.update({
        "current_target_pair_address": None,
        "current_target_symbol": None,
        "last_scan_timestamp": 0,
        "position_opened_timestamp": 0
    })
    logger.info("Bot de trade parado.")
    await update.effective_message.reply_text("🛑 Bot parado. Todas as tarefas e posições foram finalizadas.")

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

