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
MORALIS_API_KEY = os.getenv("MORALIS_API_KEY") # Chave de API da Moralis

# --- Validação de Configurações ---
if not all([TELEGRAM_TOKEN, CHAT_ID, PRIVATE_KEY_B58, RPC_URL, MORALIS_API_KEY]):
    print("Erro: Verifique se todas as variáveis de ambiente estão definidas, incluindo MORALIS_API_KEY.")
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

async def execute_buy_order(amount, price, manual=False):
    global in_position, entry_price
    if in_position:
        if manual: await send_telegram_message("⚠️ Já existe uma posição aberta.")
        return

    details = parameters["trade_pair_details"]
    logger.info(f"EXECUTANDO ORDEM DE COMPRA {'MANUAL' if manual else 'AUTOMÁTICA'} de {amount} {details['quote_symbol']} para {details['base_symbol']}")
    entry_price = price
    tx_sig = await execute_swap(details['quote_address'], details['base_address'], amount, details['quote_decimals'])
    if tx_sig:
        in_position = True
        await send_telegram_message(f"✅ COMPRA REALIZADA: {amount} {details['quote_symbol']} para {details['base_symbol']}\nhttps://solscan.io/tx/{tx_sig}")
    else:
        entry_price = 0.0
        await send_telegram_message(f"❌ FALHA NA COMPRA do token {details['base_symbol']}")

async def execute_sell_order(reason="Venda Manual"):
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
        if amount_to_sell == 0:
            logger.warning("Tentativa de venda com saldo zero."); in_position = False; entry_price = 0.0; return
        tx_sig = await execute_swap(details['base_address'], details['quote_address'], amount_to_sell, token_balance_data.decimals)
        if tx_sig:
            in_position = False
            entry_price = 0.0
            await send_telegram_message(f"🛑 VENDA REALIZADA: {amount_to_sell:.6f} de {details['base_symbol']}\nMotivo: {reason}\nhttps://solscan.io/tx/{tx_sig}")
        else:
            await send_telegram_message(f"❌ FALHA NA VENDA do token {details['base_symbol']}")
    except Exception as e:
        logger.error(f"Erro ao vender: {e}"); await send_telegram_message(f"⚠️ Falha ao vender: {e}")

# --- NOVA FUNÇÃO DE DADOS: MIGRADA PARA MORALIS (DEFINITIVO) ---
async def fetch_ohlcv_data(pair_address, timeframe):
    timeframe_map = {"1m": "1h", "5m": "1h", "15m": "1h", "1h": "1h"}
    resolution = timeframe_map.get(timeframe, "1h")

    to_date = datetime.utcnow()
    from_date = to_date - timedelta(hours=48)

    url = f"https://solana-gateway.moralis.io/token/mainnet/pairs/{pair_address}/ohlcv"
    
    params = {
        "timeframe": resolution,
        "currency": "native",
        "fromDate": from_date.strftime('%Y-%m-%d'),
        "toDate": to_date.strftime('%Y-%m-%d')
    }
    headers = {
        "X-API-KEY": MORALIS_API_KEY,
        'Cache-Control': 'no-cache'
    }

    try:
        async with httpx.AsyncClient() as client:
            logger.info(f"Chamando Moralis API: URL={url}, Params={params}") # LOG DE DEPURAÇÃO
            response = await client.get(url, params=params, headers=headers, timeout=10.0)
            logger.info(f"Resposta da Moralis (Status {response.status_code}): {response.text[:500]}") # LOG DE DEPURAÇÃO
            response.raise_for_status()
            api_data = response.json()

            if isinstance(api_data, list) and len(api_data) > 0:
                df = pd.DataFrame(api_data)
                df.rename(columns={'timestamp': 'timestamp', 'openNative': 'open', 'highNative': 'high', 'lowNative': 'low', 'closeNative': 'close', 'volumeNative': 'volume'}, inplace=True)
                df['timestamp'] = pd.to_datetime(df['timestamp'])
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = pd.to_numeric(df[col])
                return df.sort_values(by='timestamp').reset_index(drop=True)
            else:
                logger.warning(f"Moralis não retornou dados de velas ou a resposta está vazia.")
                return None
    except httpx.HTTPStatusError as e:
        logger.error(f"Erro de HTTP ao buscar dados na Moralis: {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Erro inesperado ao processar dados da Moralis: {e}")
        return None

# --- ESTRATÉGIA ---
async def check_strategy():
    global in_position, entry_price
    if not bot_running: return

    try:
        pair_details = parameters["trade_pair_details"]
        data = await fetch_ohlcv_data(pair_details['pair_address'], parameters['timeframe'])
        
        if data is None or data.empty:
            await send_telegram_message(f"⚠️ **Falha na Fonte de Dados (Moralis):**\nNão foi possível obter o histórico de velas. A API pode estar temporariamente indisponível ou este par pode não ter liquidez suficiente.")
            return
        if len(data) < parameters["lookback_period"]:
            await send_telegram_message(f"⚠️ **Dados Insuficientes (Moralis):**\nRecebidas apenas {len(data)} velas. É necessário no mínimo {parameters['lookback_period']} para uma análise segura. O par pode ter baixa liquidez.")
            return

        lookback_data = data.tail(parameters["lookback_period"]).copy()
        dynamic_support = lookback_data['low'].min()
        dynamic_resistance = lookback_data['high'].max()
        dynamic_range = dynamic_resistance - dynamic_support

        if dynamic_range == 0: return

        buy_zone_upper_limit = dynamic_support + (dynamic_range * 0.25)
        sell_zone_lower_limit = dynamic_resistance - (dynamic_range * 0.25)

        real_time_price_response = await httpx.get(f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_details['pair_address']}")
        current_price = float(real_time_price_response.json()['pairs'][0]['priceNative'])
        
        data['rsi'] = ta.rsi(data['close'], length=14)
        data['volume_sma'] = data['volume'].rolling(window=20).mean()
        
        current_rsi = data['rsi'].iloc[-1]
        current_volume = data['volume'].iloc[-1]
        volume_sma = data['volume_sma'].iloc[-1]
        
        logger.info(
            f"Análise ({pair_details['base_symbol']}): Preço {current_price:.8f} | "
            f"RSI {current_rsi:.2f} | Vol {current_volume:.2f} | Média Vol {volume_sma:.2f} | "
            f"Suporte Dinâmico {dynamic_support:.8f} | Resistência Dinâmica {dynamic_resistance:.8f}"
        )

        if in_position:
            stop_loss_price = entry_price * (1 - parameters["stop_loss_percent"] / 100)
            if current_price >= sell_zone_lower_limit:
                 await execute_sell_order(reason=f"Take Profit (Zona de Venda) atingido em {current_price:.8f}")
            elif current_price <= stop_loss_price:
                await execute_sell_order(reason=f"Stop Loss atingido em {stop_loss_price:.8f}")
        else:
            price_in_buy_zone = current_price <= buy_zone_upper_limit
            rsi_ok = current_rsi < 45
            volume_ok = current_volume > volume_sma
            if price_in_buy_zone and (rsi_ok or volume_ok):
                await execute_buy_order(parameters["amount"], current_price)
    except Exception as e:
        logger.error(f"Ocorreu um erro em check_strategy: {e}", exc_info=True)
        await send_telegram_message(f"⚠️ Erro inesperado na estratégia: {e}")

# --- Comandos do Telegram ---
async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Sou seu bot de **Range Trading Autônomo v6.2 (API Moralis)**.\n\n'
        '**Estratégia:** Esta versão final usa a API da **Moralis** para máxima fiabilidade. Por favor, adicione sua chave de API ao ficheiro `.env`.\n\n'
        'Use `/set` para configurar:\n'
        '`/set <CONTRATO> <COTAÇÃO> <TIMEFRAME> <VALOR> <LOOKBACK> <STOP_LOSS_%>`\n\n'
        '**Exemplo (BONK/SOL):**\n'
        '`/set DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 SOL 1m 0.1 30 1.5`\n\n'
        '**Comandos:**\n'
        '• `/run` - Inicia o bot.\n'
        '• `/stop` - Para o bot.\n'
        '• `/buy <VALOR>` - Compra manual.\n'
        '• `/sell` - Vende a posição atual.',
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
            f"🌐 *Fonte de Dados:* `Moralis`\n"
            f"⏰ *Timeframe:* `{timeframe}`\n"
            f"📈 *Estratégia:* Zonas Adaptativas (Lookback: {lookback_period} velas)\n"
            f"💰 *Valor por Ordem:* `{amount}` {quote_symbol_input}\n"
            f"🚀 *Taxa de Prioridade:* **Dinâmica (Automática)**\n"
            f"📉 *Stop Loss:* `{stop_loss_percent}%`",
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
    await update.effective_message.reply_text("🚀 Bot iniciado! Operando com a API Moralis.")
    
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
        await execute_buy_order(amount, 0, manual=True)
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

