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

# --- Libs da Solana ---
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from spl.token.instructions import get_associated_token_address

# --- Carrega as variáveis de ambiente (funciona localmente e no Replit/Railway) ---
load_dotenv()

# --- Configurações Iniciais ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
PRIVATE_KEY_B58 = os.getenv("PRIVATE_KEY_BASE58")
RPC_URL = os.getenv("RPC_URL")

# --- Validação de Configurações ---
if not all([TELEGRAM_TOKEN, CHAT_ID, PRIVATE_KEY_B58, RPC_URL]):
    print("Erro: Verifique se todas as variáveis de ambiente estão definidas:")
    print("TELEGRAM_TOKEN, CHAT_ID, PRIVATE_KEY_BASE58, RPC_URL")
    exit()

# --- Configuração do Logging ---
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- Cliente Solana e Carteira ---
try:
    payer = Keypair.from_base58_string(PRIVATE_KEY_B58)
    solana_client = Client(RPC_URL)
    logger.info(f"Carteira carregada com sucesso. Endereço público: {payer.pubkey()}")
except Exception as e:
    logger.error(f"Erro ao carregar a carteira Solana. Verifique sua chave privada e o RPC URL. Erro: {e}")
    exit()

# --- Variáveis Globais ---
bot_running = False
in_position = False
entry_price = 0.0
highest_price_since_entry = 0.0 # Para o Trailing Stop
check_interval_seconds = 60 # Padrão para 1 minuto
periodic_task = None
WRAPPED_SOL_MINT_ADDRESS = "So11111111111111111111111111111111111111112"
parameters = {
    "base_token_symbol": None,
    "quote_token_symbol": None,
    "timeframe": None,
    "period": None,
    "amount": None,
    "trailing_stop_percent": None,
    "trade_pair_details": {}
}
application = None

# --- Funções de Execução de Ordem (Reais e Assíncronas) ---
async def execute_swap(input_mint_str, output_mint_str, amount, input_decimals, slippage_bps=100):
    logger.info(f"Iniciando swap de {amount} do token {input_mint_str} para {output_mint_str}")
    amount_wei = int(amount * (10**input_decimals))
    
    async with httpx.AsyncClient() as client:
        try:
            quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint_str}&outputMint={output_mint_str}&amount={amount_wei}&slippageBps={slippage_bps}"
            quote_res = await client.get(quote_url)
            quote_res.raise_for_status()
            quote_response = quote_res.json()

            swap_payload = {
                "userPublicKey": str(payer.pubkey()),
                "quoteResponse": quote_response,
                "wrapAndUnwrapSol": True,
            }
            swap_url = "https://quote-api.jup.ag/v6/swap"
            swap_res = await client.post(swap_url, json=swap_payload)
            swap_res.raise_for_status()
            swap_response = swap_res.json()
            swap_tx_b64 = swap_response.get('swapTransaction')
            if not swap_tx_b64:
                logger.error(f"Erro na resposta da API de swap da Jupiter: {swap_response}"); return None

            raw_tx_bytes = b64decode(swap_tx_b64)
            swap_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
            
            signature = payer.sign_message(to_bytes_versioned(swap_tx.message))
            signed_tx = VersionedTransaction.populate(swap_tx.message, [signature])

            tx_opts = TxOpts(skip_preflight=False, preflight_commitment="confirmed")
            tx_signature = solana_client.send_raw_transaction(bytes(signed_tx), opts=tx_opts).value
            
            logger.info(f"Transação enviada com sucesso! Assinatura: {tx_signature}")
            solana_client.confirm_transaction(tx_signature, commitment="confirmed")
            logger.info(f"Transação confirmada! Link: https://solscan.io/tx/{tx_signature}")
            return str(tx_signature)

        except httpx.HTTPStatusError as e:
            logger.error(f"Erro de HTTP na API da Jupiter: {e.response.text}"); await send_telegram_message(f"⚠️ Falha na comunicação com a Jupiter: {e.response.text}"); return None
        except Exception as e:
            logger.error(f"Falha na transação: {e}"); await send_telegram_message(f"⚠️ Falha na transação on-chain: {e}"); return None

async def execute_buy_order(amount, price):
    global in_position, entry_price, highest_price_since_entry
    details = parameters["trade_pair_details"]
    logger.info(f"EXECUTANDO ORDEM DE COMPRA REAL de {amount} {details['quote_symbol']} para {details['base_symbol']} ao preço de {price}")
    
    entry_price = price
    highest_price_since_entry = price # Inicia o trailing stop

    tx_sig = await execute_swap(details['quote_address'], details['base_address'], amount, details['quote_decimals'])
    if tx_sig:
        in_position = True
        await send_telegram_message(f"✅ COMPRA REALIZADA: {amount} {details['quote_symbol']} para {details['base_symbol']}\nhttps://solscan.io/tx/{tx_sig}")
    else:
        entry_price = 0.0
        highest_price_since_entry = 0.0
        await send_telegram_message(f"❌ FALHA NA COMPRA do token {details['base_symbol']}")

async def execute_sell_order(reason="Venda Manual"):
    global in_position, entry_price, highest_price_since_entry
    details = parameters["trade_pair_details"]
    logger.info(f"EXECUTANDO ORDEM DE VENDA REAL do token {details['base_symbol']}. Motivo: {reason}")
    try:
        token_mint_pubkey = Pubkey.from_string(details['base_address'])
        ata_address = get_associated_token_address(payer.pubkey(), token_mint_pubkey)
        balance_response = solana_client.get_token_account_balance(ata_address)
        token_balance_data = balance_response.value
        amount_to_sell_wei = int(token_balance_data.amount)
        token_decimals = token_balance_data.decimals
        amount_to_sell = amount_to_sell_wei / (10**token_decimals)
        if amount_to_sell_wei == 0:
            logger.warning("Tentativa de venda com saldo zero."); in_position = False; entry_price = 0.0; highest_price_since_entry = 0.0; return
        tx_sig = await execute_swap(details['base_address'], details['quote_address'], amount_to_sell, token_decimals)
        if tx_sig:
            in_position = False
            entry_price = 0.0
            highest_price_since_entry = 0.0
            await send_telegram_message(f"🛑 VENDA REALIZADA: {amount_to_sell:.6f} de {details['base_symbol']}\nMotivo: {reason}\nhttps://solscan.io/tx/{tx_sig}")
        else:
            await send_telegram_message(f"❌ FALHA NA VENDA do token {details['base_symbol']}")
    except Exception as e:
        logger.error(f"Erro ao buscar saldo para venda: {e}"); await send_telegram_message(f"⚠️ Falha ao buscar saldo do token para venda: {e}")

# --- FUNÇÃO PARA BUSCAR DADOS DO GECKOTERMINAL (COM CORREÇÃO DE CACHE) ---
async def fetch_geckoterminal_ohlcv(pair_address, timeframe):
    timeframe_map = {"1m": "minute", "5m": "minute", "15m": "minute", "1h": "hour", "4h": "hour", "1d": "day"}
    aggregate_map = {"1m": 1, "5m": 5, "15m": 15, "1h": 1, "4h": 4, "1d": 1}
    
    gt_timeframe = timeframe_map.get(timeframe)
    gt_aggregate = aggregate_map.get(timeframe)
    
    if not gt_timeframe:
        logger.error(f"Timeframe '{timeframe}' não suportado pelo GeckoTerminal.")
        return None

    current_timestamp = int(time.time())
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair_address}/ohlcv/{gt_timeframe}?aggregate={gt_aggregate}&limit=100&before_timestamp={current_timestamp}"
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            api_data = response.json()

            if api_data.get('data') and api_data['data'].get('attributes', {}).get('ohlcv_list'):
                ohlcv_list = api_data['data']['attributes']['ohlcv_list']
                df = pd.DataFrame(ohlcv_list, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
                for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
                    df[col] = pd.to_numeric(df[col])
                return df.sort_values(by='timestamp').reset_index(drop=True)
            else:
                logger.warning(f"GeckoTerminal não retornou dados de velas. Resposta: {api_data}")
                return None
    except httpx.HTTPStatusError as e:
        logger.error(f"Erro de HTTP ao buscar dados no GeckoTerminal: {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Erro inesperado ao processar dados do GeckoTerminal: {e}")
        return None

async def check_strategy():
    global in_position, entry_price, highest_price_since_entry
    if not bot_running or not all(p is not None for p in parameters.values() if p != parameters['trade_pair_details']): return

    try:
        pair_details = parameters["trade_pair_details"]
        timeframe, period = parameters["timeframe"], int(parameters["period"])
        amount, trailing_stop_percent = parameters["amount"], parameters["trailing_stop_percent"]
        
        logger.info(f"Buscando dados de candles para {pair_details['base_symbol']}/{pair_details['quote_symbol']} no GeckoTerminal...")

        data = await fetch_geckoterminal_ohlcv(pair_details['pair_address'], timeframe)

        if data is None or data.empty:
            await send_telegram_message(f"⚠️ Não foi possível obter dados de velas do GeckoTerminal.")
            return

        # --- CÁLCULO DOS INDICADORES ---
        stoch = data.ta.stoch(length=14, append=True)
        sma = data.ta.sma(length=period, append=True)
        atr = data.ta.atr(length=14, append=True)
        
        if len(data) < 20: # Garante dados suficientes para os indicadores
            logger.warning(f"Dados insuficientes do GeckoTerminal ({len(data)} velas).")
            return

        previous_candle = data.iloc[-3]
        current_candle = data.iloc[-2]
        
        current_close = current_candle['Close']
        
        # Dados do Estocástico
        current_stoch_k = current_candle['STOCHk_14_3_3']
        current_stoch_d = current_candle['STOCHd_14_3_3']
        previous_stoch_k = previous_candle['STOCHk_14_3_3']
        previous_stoch_d = previous_candle['STOCHd_14_3_3']
        
        # Dados da Média Móvel
        current_sma = current_candle[f'SMA_{period}']
        previous_close = previous_candle['Close']
        
        # Dados do ATR (para o filtro de regime)
        current_atr = current_candle['ATRr_14']
        atr_sma = data['ATRr_14'].rolling(window=20).mean().iloc[-2] # Média do ATR para definir o regime

        # --- LÓGICA DO FILTRO DE REGIME ---
        is_trending_market = current_atr > (atr_sma * 1.5) # Se o ATR atual for 50% maior que a sua média, é um mercado de tendência
        
        market_regime = "TENDÊNCIA" if is_trending_market else "REVERSÃO"
        logger.info(f"Análise ({pair_details['base_symbol']}): Preço {current_close:.8f} | Regime: {market_regime} | ATR: {current_atr:.2f}")

        if in_position:
            # --- LÓGICA DE VENDA ---
            highest_price_since_entry = max(highest_price_since_entry, current_close)
            trailing_stop_price = highest_price_since_entry * (1 - trailing_stop_percent / 100)
            
            logger.info(f"Posição aberta. Preço de entrada: {entry_price:.8f}, Preço Máximo: {highest_price_since_entry:.8f}, Trailing Stop: {trailing_stop_price:.8f}")
            
            if current_close <= trailing_stop_price:
                await execute_sell_order(reason=f"Trailing Stop atingido em {trailing_stop_price:.8f}")
                return
            
            # Venda em Modo Reversão (Mercado Lateral)
            if not is_trending_market:
                sell_setup = current_stoch_k > 75
                sell_trigger = previous_stoch_k > previous_stoch_d and current_stoch_k < current_stoch_d
                if sell_setup and sell_trigger:
                    await execute_sell_order(reason="Sinal de Venda (Modo Reversão)")
                    return
            
            # Venda em Modo Tendência (só pelo Trailing Stop)
            # Nenhuma outra regra de venda é necessária, pois queremos "surfar" a onda.

        else: # Só procura por compras se não estiver posicionado
            # Compra em Modo Reversão (Mercado Lateral)
            if not is_trending_market:
                buy_setup = current_stoch_k < 25
                buy_trigger = previous_stoch_k < previous_stoch_d and current_stoch_k > current_stoch_d
                if buy_setup and buy_trigger:
                    logger.info("Sinal de COMPRA (Modo Reversão) detectado.")
                    await execute_buy_order(amount, current_close)
            
            # Compra em Modo Tendência
            else:
                buy_trigger = previous_close <= current_sma and current_close > current_sma
                if buy_trigger:
                    logger.info("Sinal de COMPRA (Modo Tendência) detectado.")
                    await execute_buy_order(amount, current_close)

    except Exception as e:
        logger.error(f"Ocorreu um erro em check_strategy: {e}")
        await send_telegram_message(f"⚠️ Erro inesperado ao executar a estratégia: {e}")

async def send_telegram_message(message):
    if application:
        await application.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')

async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Sou seu bot de autotrade para a rede Solana.\n'
        'Estratégia: **Modelo Híbrido (Reversão + Tendência)**.\n'
        'Fonte de Dados: **GeckoTerminal**.\n'
        'Use o comando `/set` para configurar:\n'
        '`/set <CONTRATO> <COTAÇÃO> <TIMEFRAME> <PERÍODO_MA> <VALOR> <TRAILING_STOP_%>`\n\n'
        '**Exemplo (WIF/SOL):**\n'
        '`/set EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzL7M6fV2zY2g6 SOL 5m 13 0.1 8`\n\n'
        '**Comandos:**\n'
        '• `/run` - Inicia o bot.\n'
        '• `/stop` - Para o bot.',
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
        
        timeframe, period = context.args[2].lower(), int(context.args[3])
        amount, trailing_stop_percent = float(context.args[4]), float(context.args[5])

        interval_map = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
        if timeframe not in interval_map:
            await update.effective_message.reply_text(f"⚠️ Timeframe '{timeframe}' não suportado.")
            return
        
        check_interval_seconds = interval_map[timeframe]

        token_search_url = f"https://api.dexscreener.com/latest/dex/tokens/{base_token_contract}"
        async with httpx.AsyncClient() as client:
            response = await client.get(token_search_url)
            response.raise_for_status()
            token_res = response.json()

        if not token_res.get('pairs'):
            await update.effective_message.reply_text(f"⚠️ Nenhum par encontrado no Dexscreener para o contrato fornecido.")
            return
        
        accepted_symbols = [quote_symbol_input]
        if quote_symbol_input == 'SOL':
            accepted_symbols.append('WSOL')

        valid_pairs = [p for p in token_res['pairs'] if p.get('quoteToken', {}).get('symbol') in accepted_symbols]
        
        if not valid_pairs:
            await update.effective_message.reply_text(f"⚠️ Nenhum par com `{quote_symbol_input}` encontrado para este contrato.")
            return

        trade_pair = max(valid_pairs, key=lambda p: p.get('liquidity', {}).get('usd', 0))

        base_token_symbol = trade_pair['baseToken']['symbol'].lstrip('$')
        quote_token_symbol = trade_pair['quoteToken']['symbol']

        parameters = {
            "base_token_symbol": base_token_symbol, 
            "quote_token_symbol": quote_token_symbol,
            "timeframe": timeframe, 
            "period": period,
            "amount": amount,
            "trailing_stop_percent": trailing_stop_percent,
            "trade_pair_details": {
                "base_symbol": base_token_symbol,
                "quote_symbol": quote_token_symbol,
                "base_address": trade_pair['baseToken']['address'],
                "quote_address": trade_pair['quoteToken']['address'],
                "pair_address": trade_pair['pairAddress'],
                "quote_decimals": 9 if quote_token_symbol in ['SOL', 'WSOL'] else 6 
            }
        }
        await update.effective_message.reply_text(
            f"✅ *Parâmetros definidos com sucesso!*\n\n"
            f"📊 *Fonte de Dados:* `GeckoTerminal`\n"
            f"🪙 *Par de Negociação:* `{base_token_symbol}/{quote_token_symbol}`\n"
            f"⏰ *Timeframe:* `{timeframe}`\n"
            f"📈 *Estratégia:* Modelo Híbrido (Reversão + Tendência)\n"
            f"💰 *Valor por Ordem:* `{amount}` {quote_symbol_input}\n"
            f"📉 *Trailing Stop:* `{trailing_stop_percent}%`",
            parse_mode='Markdown'
        )
    except (IndexError, ValueError):
        await update.effective_message.reply_text(
            "⚠️ *Erro: Formato incorreto.*\n"
            "Use: `/set <CONTRATO> <COTAÇÃO> <TIMEFRAME> <PERÍODO_MA> <VALOR> <TRAILING_STOP_%>`\n"
            "Exemplo: `/set ... SOL 5m 13 0.1 8`",
            parse_mode='Markdown'
        )
    except httpx.HTTPStatusError as e:
        await update.effective_message.reply_text(f"⚠️ Erro ao comunicar com a API do Dexscreener: {e}")
    except Exception as e:
        logger.error(f"Erro inesperado em set_params: {e}")
        await update.effective_message.reply_text(f"⚠️ Ocorreu um erro ao configurar os parâmetros: {e}")

async def run_bot(update, context):
    global bot_running, periodic_task
    if not all(p is not None for p in parameters.values() if p != parameters['trade_pair_details']):
        await update.effective_message.reply_text("Defina os parâmetros com /set primeiro.")
        return
    if bot_running:
        await update.effective_message.reply_text("O bot já está em execução.")
        return
    
    bot_running = True
    logger.info("Bot de trade iniciado.")
    await update.effective_message.reply_text("🚀 Bot iniciado! Verificando a estratégia Híbrida via GeckoTerminal...")
    
    if periodic_task is None or periodic_task.done():
        periodic_task = asyncio.create_task(periodic_checker())
    
    await check_strategy()

async def stop_bot(update, context):
    global bot_running, in_position, entry_price, periodic_task
    if not bot_running:
        await update.effective_message.reply_text("O bot já está parado.")
        return
    
    bot_running = False
    if periodic_task:
        periodic_task.cancel()
        periodic_task = None

    in_position, entry_price = False, 0.0
    logger.info("Bot de trade parado.")
    await update.effective_message.reply_text("🛑 Bot parado. Posição e tarefas resetadas.")

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

def main():
    global application
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set", set_params))
    application.add_handler(CommandHandler("run", run_bot))
    application.add_handler(CommandHandler("stop", stop_bot))
    
    logger.info("Bot do Telegram iniciado e aguardando comandos...")
    application.run_polling()

if __name__ == '__main__':
    main()
