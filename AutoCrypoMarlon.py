# -*- coding: utf-8 -*-
import telegram
from telegram.ext import Application, CommandHandler, BaseHandler
import logging
import time
import os
from dotenv import load_dotenv
import asyncio
from base64 import b64decode
import pandas as pd
import pandas_ta as ta
import httpx # Biblioteca para requisições assíncronas
import numpy as np # Importado para lidar com valores inválidos
from datetime import datetime, timedelta

# --- Libs da Solana ---
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from spl.token.instructions import get_associated_token_address

# --- Carrega as variáveis de ambiente ---
load_dotenv()

# --- Configurações Iniciais ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
PRIVATE_KEY_B58 = os.getenv("PRIVATE_KEY_BASE58")
RPC_URL = os.getenv("RPC_URL")

# --- Validação de Configurações ---
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

# --- Variáveis Globais ---
bot_running = False
in_position = False
entry_price = 0.0
check_interval_seconds = 60
periodic_task = None
parameters = {
    "timeframe": None,
    "amount": None,
    "stop_loss_percent": None,
    "take_profit_percent": None, # NOVO PARÂMETRO
    "trade_pair_details": {}
}
application = None

# --- FUNÇÃO OTIMIZADA: Obter Taxa de Prioridade Dinâmica ---
async def get_dynamic_priority_fee(addresses):
    try:
        async with httpx.AsyncClient() as client:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getRecentPrioritizationFees",
                "params": [[str(addr) for addr in addresses]]
            }
            response = await client.post(RPC_URL, json=payload, timeout=10.0)
            response.raise_for_status()
            result = response.json().get('result')
            if not result: return 50000
            fees = [fee['prioritizationFee'] for fee in result if fee['prioritizationFee'] > 0]
            if not fees: return 50000
            median_fee = int(np.median(fees))
            competitive_fee = int(median_fee * 1.1)
            dynamic_fee = max(50000, min(competitive_fee, 1000000))
            return dynamic_fee
    except Exception as e:
        logger.error(f"Erro ao calcular taxa de prioridade dinâmica: {e}. Usando padrão (50000).")
        return 50000

# --- Funções de Execução de Ordem ---
async def execute_swap(input_mint_str, output_mint_str, amount, input_decimals, slippage_bps=500): # Slippage aumentado para scalping
    amount_wei = int(amount * (10**input_decimals))
    involved_addresses = [Pubkey.from_string(input_mint_str), Pubkey.from_string(output_mint_str)]
    priority_fee = await get_dynamic_priority_fee(involved_addresses)
    async with httpx.AsyncClient() as client:
        try:
            quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint_str}&outputMint={output_mint_str}&amount={amount_wei}&slippageBps={slippage_bps}"
            quote_res = await client.get(quote_url)
            quote_res.raise_for_status()
            quote_response = quote_res.json()

            swap_payload = {"userPublicKey": str(payer.pubkey()), "quoteResponse": quote_response, "wrapAndUnwrapSol": True, "computeUnitPriceMicroLamports": priority_fee}
            swap_url = "https://quote-api.jup.ag/v6/swap"
            swap_res = await client.post(swap_url, json=swap_payload)
            swap_res.raise_for_status()
            swap_response = swap_res.json()
            swap_tx_b64 = swap_response.get('swapTransaction')
            if not swap_tx_b64:
                logger.error(f"Erro na API da Jupiter: {swap_response}"); return None

            raw_tx_bytes = b64decode(swap_tx_b64)
            swap_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
            signature = payer.sign_message(to_bytes_versioned(swap_tx.message))
            signed_tx = VersionedTransaction.populate(swap_tx.message, [signature])

            tx_opts = TxOpts(skip_preflight=True, preflight_commitment="processed") # Opções mais rápidas para scalping
            tx_signature = solana_client.send_raw_transaction(bytes(signed_tx), opts=tx_opts).value
            
            logger.info(f"Transação enviada: {tx_signature}")
            # Confirmação mais rápida para scalping
            await asyncio.sleep(5) # Aguarda um pouco para a transação propagar
            solana_client.confirm_transaction(tx_signature, commitment="confirmed")
            logger.info(f"Transação confirmada: https://solscan.io/tx/{tx_signature}")
            return str(tx_signature)
        except Exception as e:
            logger.error(f"Falha na transação: {e}"); await send_telegram_message(f"⚠️ Falha na transação: {e}"); return None

async def execute_buy_order(amount, price, reason, manual=False):
    global in_position, entry_price
    if in_position:
        if manual: await send_telegram_message("⚠️ Já existe uma posição aberta.")
        return

    details = parameters["trade_pair_details"]
    log_type = 'MANUAL' if manual else 'AUTOMÁTICA'
    logger.info(f"EXECUTANDO ORDEM DE COMPRA {log_type} de {amount} {details['quote_symbol']} para {details['base_symbol']}")
    
    tx_sig = await execute_swap(details['quote_address'], details['base_address'], amount, details['quote_decimals'])
    if tx_sig:
        in_position = True
        entry_price = price 
        await send_telegram_message(
            f"✅ **COMPRA REALIZADA (Scalp)**\n\n"
            f"**Par:** {details['base_symbol']}/{details['quote_symbol']}\n"
            f"**Preço de Entrada:** {price:.10f}\n"
            f"**Motivo:** {reason}\n\n"
            f"https://solscan.io/tx/{tx_sig}"
        )
    else:
        entry_price = 0.0
        await send_telegram_message(f"❌ FALHA NA COMPRA do token {details['base_symbol']}")

async def execute_sell_order(reason):
    global in_position, entry_price
    if not in_position:
        if "Manual" in reason: await send_telegram_message("⚠️ Nenhuma posição aberta para vender.")
        return
        
    details = parameters["trade_pair_details"]
    logger.info(f"EXECUTANDO ORDEM DE VENDA do token {details['base_symbol']}. Motivo: {reason}")
    try:
        token_mint_pubkey = Pubkey.from_string(details['base_address'])
        ata_address = get_associated_token_address(payer.pubkey(), token_mint_pubkey)
        balance_response = solana_client.get_token_account_balance(ata_address)
        token_balance_data = balance_response.value
        amount_to_sell = token_balance_data.ui_amount
        if amount_to_sell is None or amount_to_sell == 0:
            logger.warning("Tentativa de venda com saldo zero."); in_position = False; entry_price = 0.0; return
        
        tx_sig = await execute_swap(details['base_address'], details['quote_address'], amount_to_sell, token_balance_data.decimals)
        if tx_sig:
            profit_percent = ((solana_client.get_latest_blockhash().value.last_valid_block_height - entry_price) / entry_price) * 100 if entry_price > 0 else 0
            in_position = False
            entry_price = 0.0
            await send_telegram_message(
                f"🛑 **VENDA REALIZADA (Scalp)**\n\n"
                f"**Par:** {details['base_symbol']}/{details['quote_symbol']}\n"
                f"**Motivo:** {reason}\n\n"
                f"https://solscan.io/tx/{tx_sig}"
            )
        else:
            await send_telegram_message(f"❌ FALHA NA VENDA do token {details['base_symbol']}")
    except Exception as e:
        logger.error(f"Erro ao vender: {e}"); await send_telegram_message(f"⚠️ Falha ao vender: {e}")
        in_position = False
        entry_price = 0.0

# --- FUNÇÕES DE DADOS ---
async def fetch_geckoterminal_ohlcv(pair_address, timeframe):
    timeframe_map = {"1m": "minute", "5m": "minute", "15m": "minute", "1h": "hour"}
    aggregate_map = {"1m": 1, "5m": 5, "15m": 15, "1h": 1}
    gt_timeframe = timeframe_map.get(timeframe)
    gt_aggregate = aggregate_map.get(timeframe)
    if not gt_timeframe: return None

    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair_address}/ohlcv/{gt_timeframe}?aggregate={gt_aggregate}&limit=200&currency=usd"
    headers = {'Cache-Control': 'no-cache, no-store, must-revalidate'}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=10.0)
            response.raise_for_status()
            api_data = response.json()
            if api_data.get('data') and api_data['data'].get('attributes', {}).get('ohlcv_list'):
                df = pd.DataFrame(api_data['data']['attributes']['ohlcv_list'], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
                for col in ['open', 'high', 'low', 'close', 'volume']: df[col] = pd.to_numeric(df[col])
                return df.sort_values(by='timestamp').reset_index(drop=True)
            return None
    except Exception as e:
        logger.error(f"Erro ao processar dados do GeckoTerminal: {e}"); return None

async def fetch_dexscreener_real_time_price(pair_address):
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    headers = {'Cache-Control': 'no-cache, no-store, must-revalidate'}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=5.0)
            response.raise_for_status()
            api_data = response.json()
            if api_data.get('pair'):
                return float(api_data['pair'].get('priceNative', 0)), float(api_data['pair'].get('priceUsd', 0))
            return None, None
    except Exception as e:
        logger.error(f"Erro ao buscar preço no Dexscreener: {e}"); return None, None

# --- ESTRATÉGIA ---
# MODIFICAÇÃO PRINCIPAL: Estratégia de Scalping de Momentum com EMAs
async def check_strategy():
    global in_position, entry_price
    if not bot_running: return

    try:
        pair_details = parameters["trade_pair_details"]
        current_price_native, current_price_usd = await fetch_dexscreener_real_time_price(pair_details['pair_address'])
        if current_price_native is None:
            logger.warning("Não foi possível obter o preço em tempo real.")
            return

        if in_position:
            # --- LÓGICA DE VENDA (SAÍDA RÁPIDA) ---
            take_profit_price = entry_price * (1 + parameters["take_profit_percent"] / 100)
            stop_loss_price = entry_price * (1 - parameters["stop_loss_percent"] / 100)
            
            sell_reason = None
            if current_price_native >= take_profit_price:
                sell_reason = f"Take Profit (+{parameters['take_profit_percent']}%) atingido em {current_price_native:.10f}"
            elif current_price_native <= stop_loss_price:
                sell_reason = f"Stop Loss (-{parameters['stop_loss_percent']}%) atingido em {current_price_native:.10f}"
            
            if sell_reason:
                await execute_sell_order(reason=sell_reason)
            else: # Log de acompanhamento da posição
                profit = ((current_price_native - entry_price) / entry_price) * 100
                logger.info(
                    f"Posição Aberta ({pair_details['base_symbol']}): "
                    f"Preço Atual: {current_price_native:.10f} | "
                    f"Entrada: {entry_price:.10f} | "
                    f"Lucro/Prejuízo: {profit:+.2f}% | "
                    f"Alvo: {take_profit_price:.10f} | Stop: {stop_loss_price:.10f}"
                )
            return # Se está em posição, não avalia a compra

        # --- LÓGICA DE COMPRA (ENTRADA NO MOMENTUM) ---
        data = await fetch_geckoterminal_ohlcv(pair_details['pair_address'], parameters['timeframe'])
        if data is None or data.empty or len(data) < 20: # Precisa de dados para EMAs
            logger.warning(f"Dados históricos insuficientes para cálculo de EMAs. Aguardando...")
            return

        # Cálculos de indicadores
        data.ta.ema(length=5, append=True, col_names=('EMA_5',))
        data.ta.ema(length=10, append=True, col_names=('EMA_10',))
        data['VOL_MA_9'] = data['volume'].rolling(window=9).mean()
        data.dropna(inplace=True)
        if len(data) < 2: return # Garante que temos pelo menos 2 pontos para comparar

        # Pega os valores mais recentes
        last = data.iloc[-1]
        prev = data.iloc[-2]
        
        # Log de Análise para Compra
        logger.info(
            f"Análise Compra ({pair_details['base_symbol']}): EMA_5={last['EMA_5']:.10f} | EMA_10={last['EMA_10']:.10f} | "
            f"Volume={last['volume']:.2f} | Média Vol(9)={last['VOL_MA_9']:.2f}"
        )
        
        # Condições da estratégia
        ema_crossed_up = last['EMA_5'] > last['EMA_10'] and prev['EMA_5'] <= prev['EMA_10']
        volume_is_high = last['volume'] > last['VOL_MA_9']
        
        if ema_crossed_up and volume_is_high:
            reason = f"Cruzamento de EMA (5>10) com Volume ({last['volume']:.2f}) > Média ({last['VOL_MA_9']:.2f})"
            await execute_buy_order(parameters["amount"], current_price_native, reason=reason)

    except Exception as e:
        logger.error(f"Ocorreu um erro em check_strategy: {e}", exc_info=True)
        await send_telegram_message(f"⚠️ Erro inesperado na estratégia: {e}")

# --- Comandos do Telegram ---
async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Sou seu bot de **Trading Autônomo v11.0 (Scalping de Momentum)**.\n\n'
        '**Filosofia:** Realizar muitas operações curtas, buscando lucros pequenos e controlando as perdas de forma rigorosa.\n\n'
        '**Estratégia de Compra:**\n'
        '• A Média Móvel Exponencial (EMA) de 5 períodos cruza para **CIMA** da EMA de 10 períodos.\n'
        '• O volume da vela atual é **MAIOR** que a média do volume das últimas 9 velas.\n\n'
        '**Estratégia de Venda:**\n'
        '• Atingir a meta de lucro (Take Profit) ou o limite de perda (Stop Loss) definidos.\n\n'
        '**NOVO FORMATO DO COMANDO /SET:**\n'
        '`/set <CONTRATO> <COTAÇÃO> <TIMEFRAME> <VALOR> <STOP_LOSS_%> <TAKE_PROFIT_%>`\n\n'
        '**Exemplo:**\n'
        '`/set DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 SOL 1m 0.1 0.75 1.5`\n'
        '(Stop de -0.75% e Alvo de +1.5%)',
        parse_mode='Markdown'
    )

async def set_params(update, context):
    global parameters
    if bot_running:
        await update.effective_message.reply_text("Pare o bot com /stop antes de alterar os parâmetros.")
        return
    try:
        # NOVO FORMATO: 6 argumentos
        base_token_contract = context.args[0]
        quote_symbol_input = context.args[1].upper()
        timeframe = context.args[2].lower()
        amount = float(context.args[3])
        stop_loss_percent = float(context.args[4])
        take_profit_percent = float(context.args[5])
        
        # Otimização de intervalo para scalping
        global check_interval_seconds
        interval_map = {"1m": 15, "5m": 60, "15m": 300, "1h": 900}
        check_interval_seconds = interval_map.get(timeframe, 15) # Verifica a cada 15s para timeframe de 1m

        # Validações
        if stop_loss_percent <= 0 or take_profit_percent <= 0:
            await update.effective_message.reply_text("⚠️ Stop Loss e Take Profit devem ser valores positivos.")
            return

        # ... (restante da busca de token é igual) ...
        async with httpx.AsyncClient() as client:
            response = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{base_token_contract}")
            response.raise_for_status()
            token_res = response.json()
        
        valid_pairs = [p for p in token_res.get('pairs', []) if p.get('quoteToken', {}).get('symbol') in [quote_symbol_input, 'WSOL' if quote_symbol_input == 'SOL' else None]]
        if not valid_pairs:
            await update.effective_message.reply_text(f"⚠️ Nenhum par com `{quote_symbol_input}` encontrado."); return

        trade_pair = max(valid_pairs, key=lambda p: p.get('liquidity', {}).get('usd', 0))

        parameters = {
            "timeframe": timeframe,
            "amount": amount,
            "stop_loss_percent": stop_loss_percent,
            "take_profit_percent": take_profit_percent,
            "trade_pair_details": {
                "base_symbol": trade_pair['baseToken']['symbol'].lstrip('$'),
                "quote_symbol": trade_pair['quoteToken']['symbol'],
                "base_address": trade_pair['baseToken']['address'],
                "quote_address": trade_pair['quoteToken']['address'],
                "pair_address": trade_pair['pairAddress'],
                "quote_decimals": 9 if trade_pair['quoteToken']['symbol'] in ['SOL', 'WSOL'] else 6
            }
        }
        await update.effective_message.reply_text(
            f"✅ *Parâmetros de Scalping definidos!*\n\n"
            f"📊 *Par:* `{parameters['trade_pair_details']['base_symbol']}/{parameters['trade_pair_details']['quote_symbol']}`\n"
            f"⏰ *Timeframe:* `{timeframe}` (Verificação a cada {check_interval_seconds}s)\n"
            f"📈 *Estratégia:* **Cruzamento de EMA (5/10) + Pico de Volume**\n"
            f"💰 *Valor por Ordem:* `{amount}` {quote_symbol_input}\n"
            f"🛑 *Stop Loss:* `-{stop_loss_percent}%`\n"
            f"🎯 *Take Profit:* `+{take_profit_percent}%`",
            parse_mode='Markdown'
        )
    except (IndexError, ValueError):
        await update.effective_message.reply_text(
            "⚠️ *Formato incorreto.*\n"
            "Use: `/set <CONTRATO> <COTAÇÃO> <TIMEFRAME> <VALOR> <STOP_LOSS_%> <TAKE_PROFIT_%>`",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Erro em set_params: {e}")
        await update.effective_message.reply_text(f"⚠️ Erro ao configurar: {e}")

# ... (restante do código: run_bot, stop_bot, buy/sell_manual, etc. permanece funcionalmente o mesmo) ...
async def run_bot(update, context):
    global bot_running, periodic_task
    if "take_profit_percent" not in parameters or parameters["take_profit_percent"] is None:
        await update.effective_message.reply_text("Defina os parâmetros com /set primeiro.")
        return
    if bot_running:
        await update.effective_message.reply_text("O bot já está em execução.")
        return
    
    bot_running = True
    logger.info("Bot de trade iniciado.")
    await update.effective_message.reply_text("🚀 Bot de Scalping iniciado!")
    
    if periodic_task is None or periodic_task.done():
        periodic_task = asyncio.create_task(periodic_checker())
    await check_strategy()

async def stop_bot(update, context):
    global bot_running, periodic_task, in_position, entry_price
    if not bot_running:
        await update.effective_message.reply_text("O bot já está parado.")
        return
    
    bot_running = False
    if periodic_task:
        periodic_task.cancel()
        periodic_task = None

    in_position, entry_price = False, 0.0
    logger.info("Bot de trade parado.")
    await update.effective_message.reply_text("🛑 Bot parado.")
    
async def buy_manual(update, context):
    try:
        amount = float(context.args[0])
        details = parameters.get("trade_pair_details")
        if not details:
            await update.effective_message.reply_text("⚠️ Configure o par com /set primeiro.")
            return
        current_price, _ = await fetch_dexscreener_real_time_price(details['pair_address'])
        if current_price is None:
             await update.effective_message.reply_text("⚠️ Não foi possível obter o preço atual para a compra manual.")
             return
        await execute_buy_order(amount, current_price, reason="Comando /buy manual", manual=True)
    except (IndexError, ValueError):
        await update.effective_message.reply_text("⚠️ *Formato incorreto.* Use: `/buy <VALOR>` (ex: `/buy 0.1`)")
    except Exception as e:
        logger.error(f"Erro no comando /buy: {e}")
        await update.effective_message.reply_text(f"⚠️ Erro ao executar compra manual: {e}")

async def sell_manual(update, context):
    await execute_sell_order(reason="Venda Manual")

async def periodic_checker():
    while True:
        try:
            if bot_running:
                logger.info("Executando verificação periódica da estratégia...")
                await check_strategy()
            # O intervalo de sleep agora é dinâmico
            await asyncio.sleep(check_interval_seconds)
        except asyncio.CancelledError:
            logger.info("Verificador periódico cancelado."); break
        except Exception as e:
            logger.error(f"Erro no loop do verificador periódico: {e}")
            await asyncio.sleep(60)

async def error_handler(update, context):
    logger.error(f"Exceção ao manusear uma atualização: {context.error}", exc_info=context.error)

async def send_telegram_message(message):
    if application:
        await application.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')

def main():
    global application
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set", set_params))
    application.add_handler(CommandHandler("run", run_bot))
    application.add_handler(CommandHandler("stop", stop_bot))
    application.add_handler(CommandHandler("buy", buy_manual))
    application.add_handler(CommandHandler("sell", sell_manual))
    application.add_error_handler(error_handler)
    
    logger.info("Bot do Telegram iniciado e aguardando comandos...")
    application.run_polling()

if __name__ == '__main__':
    main()
