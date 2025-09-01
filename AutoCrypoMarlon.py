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
    "lookback_period": None,
    "timeframe": None,
    "amount": None,
    "stop_loss_percent": None,
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
            logger.info(f"Taxa de prioridade dinâmica calculada: {dynamic_fee} micro-lamports")
            return dynamic_fee
    except Exception as e:
        logger.error(f"Erro ao calcular taxa de prioridade dinâmica: {e}. Usando padrão (50000).")
        return 50000

# --- Funções de Execução de Ordem ---
async def execute_swap(input_mint_str, output_mint_str, amount, input_decimals, slippage_bps=250):
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

            tx_opts = TxOpts(skip_preflight=False, preflight_commitment="confirmed")
            tx_signature = solana_client.send_raw_transaction(bytes(signed_tx), opts=tx_opts).value
            
            logger.info(f"Transação enviada: {tx_signature}")
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
            f"✅ **COMPRA REALIZADA**\n\n"
            f"**Par:** {details['base_symbol']}/{details['quote_symbol']}\n"
            f"**Valor:** {amount} {details['quote_symbol']}\n"
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
            logger.warning("Tentativa de venda com saldo zero ou nulo."); in_position = False; entry_price = 0.0; return
        
        tx_sig = await execute_swap(details['base_address'], details['quote_address'], amount_to_sell, token_balance_data.decimals)
        if tx_sig:
            in_position = False
            entry_price = 0.0
            await send_telegram_message(
                f"🛑 **VENDA REALIZADA**\n\n"
                f"**Par:** {details['base_symbol']}/{details['quote_symbol']}\n"
                f"**Quantidade:** {amount_to_sell:.6f} {details['base_symbol']}\n"
                f"**Motivo:** {reason}\n\n"
                f"https://solscan.io/tx/{tx_sig}"
            )
        else:
            await send_telegram_message(f"❌ FALHA NA VENDA do token {details['base_symbol']}")
    except Exception as e:
        logger.error(f"Erro ao vender: {e}"); await send_telegram_message(f"⚠️ Falha ao vender: {e}")
        in_position = False
        entry_price = 0.0

# --- FUNÇÕES DE DADOS (HÍBRIDAS) ---
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
                ohlcv_list = api_data['data']['attributes']['ohlcv_list']
                df = pd.DataFrame(ohlcv_list, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = pd.to_numeric(df[col])
                return df.sort_values(by='timestamp').reset_index(drop=True)
            return None
    except Exception as e:
        logger.error(f"Erro ao processar dados do GeckoTerminal: {e}")
        return None

async def fetch_dexscreener_real_time_price(pair_address):
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    headers = {'Cache-Control': 'no-cache, no-store, must-revalidate'}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=5.0)
            response.raise_for_status()
            api_data = response.json()
            if api_data.get('pair'):
                price_native = float(api_data['pair'].get('priceNative', 0))
                price_usd = float(api_data['pair'].get('priceUsd', 0))
                return price_native, price_usd
            return None, None
    except Exception as e:
        logger.error(f"Erro ao buscar preço no Dexscreener: {e}")
        return None, None

# --- ESTRATÉGIA ---
# MODIFICAÇÃO PRINCIPAL: Lógica de compra simplificada para faixa de RSI
async def check_strategy():
    global in_position, entry_price
    if not bot_running: return

    try:
        pair_details = parameters["trade_pair_details"]
        data = await fetch_geckoterminal_ohlcv(pair_details['pair_address'], parameters['timeframe'])
        
        if data is None or data.empty or len(data) < 30:
            logger.warning(f"Dados históricos insuficientes (necessário ~30 velas, obtido {len(data)}). Aguardando...")
            return

        current_price_native, current_price_usd = await fetch_dexscreener_real_time_price(pair_details['pair_address'])
        if current_price_native is None or current_price_usd is None:
            await send_telegram_message("⚠️ **Falha no Preço em Tempo Real (Dexscreener):**\nNão foi possível obter o preço atual.")
            return
        
        data['rsi'] = ta.rsi(data['close'], length=14)
        data.dropna(subset=['rsi'], inplace=True)
        if len(data) < 15: 
            logger.warning("Não há dados de RSI suficientes para continuar a análise.")
            return

        current_rsi = data['rsi'].iloc[-1]
        current_volume = data['volume'].iloc[-1]
        
        # --- Log de Análise Detalhado ---
        logger.info(
            f"Análise ({pair_details['base_symbol']}): "
            f"Preço: ${current_price_usd:.10f} USD ({current_price_native:.10f} {pair_details['quote_symbol']}) | "
            f"RSI: {current_rsi:.2f} | "
            f"Volume: {current_volume:.2f}"
        )
        logger.info(
            f"--> Critérios | Compra: RSI na faixa (35-48) + 3xVol>1k | Venda: RSI >= 52 + 3xVol>1k"
        )
        
        # --- Lógica de Decisão ---
        buy_reason = None
        sell_reason = None

        vol_1 = data['volume'].iloc[-1]
        vol_2 = data['volume'].iloc[-2]
        vol_3 = data['volume'].iloc[-3]
        sustained_high_volume = (vol_1 > 1000 and vol_2 > 1000 and vol_3 > 1000)

        if not in_position:
            # --- LÓGICA DE COMPRA (ESTRATÉGIA DE FAIXA DE RSI) ---
            if (current_rsi > 35 and current_rsi <= 48 and sustained_high_volume):
                buy_reason = (f"RSI ({current_rsi:.2f}) na faixa de compra (35-48) "
                              f"com volume alto sustentado.")
                await execute_buy_order(parameters["amount"], current_price_native, reason=buy_reason)
        
        else: # Já está em posição, procurar por VENDA
            stop_loss_price = entry_price * (1 - parameters["stop_loss_percent"] / 100)
            if current_price_native <= stop_loss_price:
                sell_reason = f"Stop Loss Fixo ({parameters['stop_loss_percent']}%) atingido em {current_price_native:.10f} (Entrada: {entry_price:.10f})"
            
            elif current_rsi >= 52 and sustained_high_volume:
                sell_reason = (f"Sinal de Venda: RSI ({current_rsi:.2f}) >= 52 e 3 velas com volume > 1000 "
                               f"([{vol_1:.2f}, {vol_2:.2f}, {vol_3:.2f}])")
            
            if sell_reason:
                await execute_sell_order(reason=sell_reason)
    
    except Exception as e:
        logger.error(f"Ocorreu um erro em check_strategy: {e}", exc_info=True)
        await send_telegram_message(f"⚠️ Erro inesperado na estratégia: {e}")

# --- Comandos do Telegram ---
async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Sou seu bot de **Trading Autônomo v10.3 (Estratégia de Faixa de RSI)**.\n\n'
        '**Estratégia de Compra:**\n'
        '• RSI deve estar na faixa entre `35` e `48` E\n'
        '• As 3 últimas velas devem ter volume > 1000.\n\n'
        '**Estratégia de Venda:**\n'
        '• RSI deve ser `≥ 52` com 3 velas de volume alto OU\n'
        '• O stop-loss fixo é atingido.\n\n'
        'Use `/set` para configurar:\n'
        '`/set <CONTRATO> <COTAÇÃO> <TIMEFRAME> <VALOR> <LOOKBACK> <STOP_LOSS_%>`',
        parse_mode='Markdown'
    )

async def set_params(update, context):
    global parameters, bot_running, check_interval_seconds
    if bot_running:
        await update.effective_message.reply_text("Pare o bot com /stop antes de alterar os parâmetros.")
        return
    try:
        base_token_contract = context.args[0]
        quote_symbol_input = context.args[1].upper()
        timeframe = context.args[2].lower()
        amount, lookback_period, stop_loss_percent = float(context.args[3]), int(context.args[4]), float(context.args[5])
        
        interval_map = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
        check_interval_seconds = interval_map.get(timeframe, 60)

        token_search_url = f"https://api.dexscreener.com/latest/dex/tokens/{base_token_contract}"
        async with httpx.AsyncClient() as client:
            response = await client.get(token_search_url)
            response.raise_for_status()
            token_res = response.json()
        
        if not token_res.get('pairs'):
            await update.effective_message.reply_text(f"⚠️ Nenhum par encontrado para o contrato.")
            return
        
        accepted_symbols = [quote_symbol_input]
        if quote_symbol_input == 'SOL': accepted_symbols.append('WSOL')

        valid_pairs = [p for p in token_res['pairs'] if p.get('quoteToken', {}).get('symbol') in accepted_symbols]
        if not valid_pairs:
            await update.effective_message.reply_text(f"⚠️ Nenhum par com `{quote_symbol_input}` encontrado.")
            return

        trade_pair = max(valid_pairs, key=lambda p: p.get('liquidity', {}).get('usd', 0))
        base_token_symbol = trade_pair['baseToken']['symbol'].lstrip('$')
        quote_token_symbol = trade_pair['quoteToken']['symbol']

        parameters = {
            "timeframe": timeframe,
            "amount": amount,
            "lookback_period": lookback_period,
            "stop_loss_percent": stop_loss_percent,
            "trade_pair_details": {
                "base_symbol": base_token_symbol,
                "quote_symbol": quote_token_symbol,
                "base_address": trade_pair['baseToken']['address'],
                "quote_address": trade_pair['quoteToken']['address'],
                "pair_address": trade_pair['pairAddress'],
                "quote_decimals": 9 if quote_token_symbol in ['SOL', 'WSOL'] else 5
            }
        }
        await update.effective_message.reply_text(
            f"✅ *Parâmetros definidos!*\n\n"
            f"📊 *Par:* `{base_token_symbol}/{quote_token_symbol}`\n"
            f"⏰ *Timeframe:* `{timeframe}`\n"
            f"📈 *Estratégia:* **Faixa de RSI + Volume**\n"
            f"💰 *Valor por Ordem:* `{amount}` {quote_symbol_input}\n"
            f"🚀 *Taxa de Prioridade:* **Dinâmica (Automática)**\n"
            f"📉 *Stop Loss Fixo:* `{stop_loss_percent}%`",
            parse_mode='Markdown'
        )
    except (IndexError, ValueError):
        await update.effective_message.reply_text(
            "⚠️ *Formato incorreto.*\n"
            "Use: `/set <CONTRATO> <COTAÇÃO> <TIMEFRAME> <VALOR> <LOOKBACK> <STOP_LOSS_%>`",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Erro em set_params: {e}")
        await update.effective_message.reply_text(f"⚠️ Erro ao configurar: {e}")

async def run_bot(update, context):
    global bot_running, periodic_task
    if "lookback_period" not in parameters or parameters["lookback_period"] is None:
        await update.effective_message.reply_text("Defina os parâmetros com /set primeiro.")
        return
    if bot_running:
        await update.effective_message.reply_text("O bot já está em execução.")
        return
    
    bot_running = True
    logger.info("Bot de trade iniciado.")
    await update.effective_message.reply_text("🚀 Bot iniciado! Operando com a Estratégia de Faixa de RSI.")
    
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
    await execute_sell_order(reason="Comando /sell manual")

# --- Loop Principal e Inicialização ---
async def periodic_checker():
    logger.info(f"Verificador periódico iniciado com intervalo de {check_interval_seconds} segundos.")
    while True:
        try:
            await asyncio.sleep(check_interval_seconds)
            if bot_running:
                logger.info("Executando verificação periódica da estratégia...")
                await check_strategy()
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
