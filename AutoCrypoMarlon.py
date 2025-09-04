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
    "penalty_box": {},
    "discovered_pairs": {}
}

parameters = {
    "timeframe": "1m",
    "amount": None,
    "stop_loss_percent": None,
    "take_profit_percent": None,
}

# --- Funções de Execução de Ordem ---
async def execute_swap(input_mint_str, output_mint_str, amount, input_decimals, slippage_bps):
    logger.info(f"Iniciando swap de {amount} do token {input_mint_str} para {output_mint_str} com slippage de {slippage_bps} BPS")
    amount_wei = int(amount * (10**input_decimals))
    
    async with httpx.AsyncClient() as client:
        try:
            quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint_str}&outputMint={output_mint_str}&amount={amount_wei}&slippageBps={slippage_bps}&maxAccounts=64"
            quote_res = await client.get(quote_url, timeout=60.0)
            quote_res.raise_for_status()
            quote_response = quote_res.json()

            swap_payload = { "userPublicKey": str(payer.pubkey()), "quoteResponse": quote_response, "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True }
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
            return str(tx_signature)
        except Exception as e:
            logger.error(f"Falha na transação: {e}"); await send_telegram_message(f"⚠️ Falha na transação: {e}"); return None

async def execute_buy_order(amount, price, pair_details, manual=False):
    global in_position, entry_price
    if in_position: return

    reason = "Ordem Manual" if manual else "Sinal de Pullback na EMA 5"

    slippage_bps = await calculate_dynamic_slippage(pair_details['pair_address'])
    logger.info(f"EXECUTANDO ORDEM DE COMPRA de {amount} SOL para {pair_details['base_symbol']} ao preço de {price}")
    
    tx_sig = await execute_swap(pair_details['quote_address'], pair_details['base_address'], amount, 9, slippage_bps)
    if tx_sig:
        in_position = True
        entry_price = price
        automation_state["position_opened_timestamp"] = time.time()
        log_message = (f"✅ COMPRA REALIZADA: {amount} SOL para {pair_details['base_symbol']}\n"
                       f"Motivo: {reason}\n"
                       f"Entrada: {price:.10f} | Alvo: {price * (1 + parameters['take_profit_percent']/100):.10f} | "
                       f"Stop: {price * (1 - parameters['stop_loss_percent']/100):.10f}\n"
                       f"Slippage Usado: {slippage_bps/100:.2f}%\n"
                       f"https://solscan.io/tx/{tx_sig}")
        logger.info(log_message)
        await send_telegram_message(log_message)
    else:
        # --- LÓGICA DE FALHA E PENALIZAÇÃO ---
        logger.error(f"FALHA NA EXECUÇÃO da compra para {pair_details['base_symbol']}. Penalizando e procurando novo alvo.")
        await send_telegram_message(f"❌ FALHA NA EXECUÇÃO da compra para **{pair_details['base_symbol']}**. A moeda será penalizada.")
        
        # Penaliza a moeda e força um re-scan no próximo ciclo do loop
        automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
        automation_state["current_target_pair_address"] = None

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
            logger.warning("Tentativa de venda com saldo zero, resetando posição.")
            in_position = False; entry_price = 0.0; automation_state["position_opened_timestamp"] = 0; automation_state["current_target_pair_address"] = None
            return

        slippage_bps = await calculate_dynamic_slippage(pair_details['pair_address'])
        tx_sig = await execute_swap(pair_details['base_address'], pair_details['quote_address'], amount_to_sell, token_balance_data.decimals, slippage_bps)
        
        if tx_sig:
            log_message = f"🛑 VENDA REALIZADA: {symbol}\nMotivo: {reason}\nSlippage Usado: {slippage_bps/100:.2f}%\nhttps://solscan.io/tx/{tx_sig}"
            logger.info(log_message)
            await send_telegram_message(log_message)
            in_position = False; entry_price = 0.0; automation_state["position_opened_timestamp"] = 0; automation_state["current_target_pair_address"] = None
        else:
            logger.error(f"FALHA NA VENDA do token {symbol}. O bot permanecerá em posição e tentará vender novamente.")
            await send_telegram_message(f"❌ FALHA NA VENDA do token {symbol}. O bot tentará novamente.")
    except Exception as e:
        logger.error(f"Erro crítico ao vender {symbol}: {e}")
        await send_telegram_message(f"⚠️ Erro crítico ao vender {symbol}: {e}. O bot permanecerá em posição.")

# --- Funções de Análise e Descoberta (e outras auxiliares) ---
# ... (Nenhuma alteração nestas funções) ...

# --- Estratégia ---
async def check_pullback_strategy():
    # ... (Nenhuma alteração aqui) ...

# --- Loop Principal Autônomo ---
# ... (Nenhuma alteração aqui) ...

# --- Comandos do Telegram ---
async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Sou seu bot **v19.1 (Execução Robusta)**.\n\n'
        '**Dinâmica Autônoma:**\n'
        '1. Descubro e seleciono a melhor moeda para operar.\n'
        '2. **(NOVO)** Se uma compra falhar na execução, a moeda é penalizada e eu procuro um novo alvo.\n'
        '3. Após fechar qualquer operação, eu imediatamente procuro uma nova oportunidade.\n\n'
        '**Estratégia:** Pullback na EMA 5.\n\n'
        '**Use `/set` e `/run`.**\n'
        '`/set <VALOR> <STOP_LOSS_%> <TAKE_PROFIT_%>`\n\n'
        '**Comandos Manuais:**\n'
        '`/buy <valor>` e `/sell`',
        parse_mode='Markdown'
    )

# ... (Restante do código, como set_params, run_bot, stop_bot, main, etc., permanece o mesmo) ...
