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
    "last_scan_timestamp": 0,
    "position_opened_timestamp": 0,
    "discovered_pairs": {} # Armazena os pares descobertos
}

parameters = {
    "timeframe": "1m",
    "amount": None,
    "stop_loss_percent": None,
    "take_profit_percent": None
}

# --- Funções de Execução de Ordem (sem alterações) ---
# ... (execute_swap, execute_buy_order, execute_sell_order) ...
async def execute_swap(input_mint_str, output_mint_str, amount, input_decimals, slippage_bps=500):
    logger.info(f"Iniciando swap de {amount} do token {input_mint_str} para {output_mint_str}")
    amount_wei = int(amount * (10**input_decimals))
    priority_fee_instructions = [compute_budget.set_compute_unit_price(1_000_000)] # Exemplo de taxa de prioridade
    
    async with httpx.AsyncClient() as client:
        # ... (código do swap)
        pass # Mantido igual à versão anterior para brevidade

async def execute_buy_order(amount, price, pair_details):
    global in_position, entry_price
    if in_position: return
    
    logger.info(f"EXECUTANDO ORDEM DE COMPRA de {amount} SOL para {pair_details['base_symbol']} ao preço de {price}")
    # ... (lógica de swap)
    in_position = True
    entry_price = price
    automation_state["position_opened_timestamp"] = time.time()
    log_message = (f"✅ COMPRA REALIZADA: {amount} SOL para {pair_details['base_symbol']}\n"
                   f"Entrada: {price:.10f} | Alvo: {price * (1 + parameters['take_profit_percent']/100):.10f} | "
                   f"Stop: {price * (1 - parameters['stop_loss_percent']/100):.10f}")
    logger.info(log_message)
    await send_telegram_message(log_message)

async def execute_sell_order(reason=""):
    global in_position, entry_price
    if not in_position: return
    
    pair_details = automation_state.get('current_target_pair_details', {})
    symbol = pair_details.get('base_symbol', 'TOKEN')
    logger.info(f"EXECUTANDO ORDEM DE VENDA de {symbol}. Motivo: {reason}")
    # ... (lógica de swap)
    in_position = False
    entry_price = 0.0
    automation_state["position_opened_timestamp"] = 0
    log_message = f"🛑 VENDA REALIZADA: {symbol}\nMotivo: {reason}"
    logger.info(log_message)
    await send_telegram_message(log_message)
    
# --- Funções de Análise e Descoberta ---
async def fetch_geckoterminal_ohlcv(pair_address, timeframe, limit=60):
    # ... (código inalterado) ...
    pass

async def fetch_dexscreener_real_time_price(pair_address):
    # ... (código inalterado) ...
    pass

async def get_pair_details(pair_address):
    # ... (código inalterado) ...
    pass

async def discover_and_filter_pairs():
    """Busca os pares mais promissores no Dexscreener e os filtra."""
    logger.info("--- FASE 1: DESCOBERTA --- Buscando e filtrando os melhores pares...")
    # NOTA: Este endpoint de busca do Dexscreener não é oficialmente documentado e pode mudar.
    url = "https://io.dexscreener.com/u/search/pairs?q=sol"
    
    filtered_pairs = {}
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=20.0)
            res.raise_for_status()
            pairs = res.json().get('pairs', [])
            
            logger.info(f"Encontrados {len(pairs)} pares populares. Aplicando filtros...")
            
            for pair in pairs:
                try:
                    liquidity = float(pair.get('liquidity', {}).get('usd', 0))
                    volume_24h = float(pair.get('volume', {}).get('h24', 0))
                    age_ms = pair.get('pairCreatedAt', 0)
                    age_hours = (time.time() * 1000 - age_ms) / (1000 * 60 * 60) if age_ms else 0
                    
                    if (pair.get('quoteToken',{}).get('symbol') == 'SOL' and
                        liquidity > 200000 and
                        volume_24h > 1000000 and
                        age_hours > 2):
                        
                        symbol = pair['baseToken']['symbol']
                        address = pair['pairAddress']
                        filtered_pairs[symbol] = address
                except (ValueError, TypeError):
                    continue # Ignora pares com dados malformados

        logger.info(f"Descoberta finalizada. {len(filtered_pairs)} pares passaram nos filtros de qualidade.")
        return filtered_pairs
    except Exception as e:
        logger.error(f"Erro ao descobrir pares no Dexscreener: {e}")
        return {}


async def analyze_and_score_coin(pair_address, symbol):
    """Analisa uma moeda e retorna uma pontuação de oportunidade."""
    # ... (código de pontuação permanece o mesmo, apenas o logging muda) ...
    score, details = 0, None
    # Lógica de pontuação...
    return score, details

async def find_best_coin_to_trade(candidate_pairs):
    """Escaneia a lista de candidatos e retorna o melhor para operar."""
    logger.info("--- FASE 2: SELEÇÃO --- Pontuando os melhores pares...")
    best_score = -1
    best_coin_info = None

    tasks = [analyze_and_score_coin(addr, symbol) for symbol, addr in candidate_pairs.items()]
    results = await asyncio.gather(*tasks)

    for (symbol, addr), (score, details) in zip(candidate_pairs.items(), results):
        if details and score > 0:
            logger.info(f"Candidato: {symbol} | Pontuação de Oportunidade: {score:.2f}")
            if score > best_score:
                best_score = score
                best_coin_info = {"symbol": symbol, "pair_address": addr, "score": score, "details": details}
    
    if best_coin_info:
        logger.info(f"--- SELEÇÃO FINALIZADA --- Melhor moeda: {best_coin_info['symbol']} (Pontuação: {best_coin_info['score']:.2f})")
    else:
        logger.warning("--- SELEÇÃO FINALIZADA --- Nenhuma moeda com oportunidade clara encontrada no momento.")
    
    return best_coin_info

# --- Estratégia de Scalping (Fase 3) ---
async def check_scalping_strategy():
    # ... (código da estratégia permanece o mesmo, mas agora é a FASE 3) ...
    pass

# --- Loop Principal Autônomo (Refatorado) ---
async def autonomous_loop():
    global bot_running
    logger.info("Loop de caça autônoma iniciado.")
    
    while bot_running:
        try:
            now = time.time()
            
            # 1. Re-descobre e re-avalia a cada 2 horas
            if now - automation_state["last_scan_timestamp"] > 7200:
                discovered_pairs = await discover_and_filter_pairs()
                automation_state["discovered_pairs"] = discovered_pairs
                
                best_coin = await find_best_coin_to_trade(discovered_pairs)
                automation_state["last_scan_timestamp"] = now

                if best_coin and best_coin["pair_address"] != automation_state.get("current_target_pair_address"):
                    logger.info(f"Novo alvo com maior potencial encontrado: {best_coin['symbol']}.")
                    if in_position:
                        await execute_sell_order(reason=f"Trocando para {best_coin['symbol']}")
                    
                    automation_state["current_target_pair_address"] = best_coin["pair_address"]
                    automation_state["current_target_symbol"] = best_coin["symbol"]
                    automation_state["current_target_pair_details"] = best_coin["details"]
                    await send_telegram_message(f"🎯 **Novo Alvo:** {best_coin['symbol']}. Procurando por entradas...")

            # 2. Se não tem alvo, tenta encontrar um
            if not automation_state.get("current_target_pair_address"):
                if not automation_state.get("discovered_pairs"):
                    automation_state["discovered_pairs"] = await discover_and_filter_pairs()
                
                best_coin = await find_best_coin_to_trade(automation_state["discovered_pairs"])
                automation_state["last_scan_timestamp"] = now
                if best_coin:
                    automation_state["current_target_pair_address"] = best_coin["pair_address"]
                    automation_state["current_target_symbol"] = best_coin["symbol"]
                    automation_state["current_target_pair_details"] = best_coin["details"]
                    await send_telegram_message(f"🎯 **Alvo Definido:** {best_coin['symbol']}. Iniciando operações.")
            
            # 3. Executa a estratégia no alvo atual
            await check_scalping_strategy()

            # Define o intervalo de sleep
            sleep_interval = 15 if in_position else 30
            await asyncio.sleep(sleep_interval)
            
        except asyncio.CancelledError:
            logger.info("Loop autônomo cancelado."); break
        except Exception as e:
            logger.error(f"Erro crítico no loop autônomo: {e}", exc_info=True)
            await asyncio.sleep(60)

# --- Comandos do Telegram (Simplificados) ---
async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Sou seu bot **v13.0 (Discovery Mode)**.\n\n'
        '**Dinâmica Autônoma:**\n'
        'Eu agora **descubro, analiso e seleciono** as melhores moedas para operar por conta própria, trocando de alvo a cada 2 horas se encontrar uma oportunidade melhor.\n\n'
        '**Gerenciamento de Risco:**\n'
        'Posições abertas por mais de 30 minutos são fechadas automaticamente para buscar novas oportunidades.\n\n'
        '**Configure-me uma vez com `/set` e depois use `/run`.**\n'
        '`/set <VALOR> <STOP_LOSS_%> <TAKE_PROFIT_%>`\n'
        '**Ex:** `/set 0.1 1.0 1.5`',
        parse_mode='Markdown'
    )

async def set_params(update, context):
    # ... (código do set_params simplificado permanece o mesmo) ...
    pass
    
# ... (run_bot, stop_bot, e main permanecem os mesmos) ...
async def run_bot(update, context):
    # ...
    pass

async def stop_bot(update, context):
    # ...
    pass

def main():
    # ...
    pass

if __name__ == '__main__':
    main()
