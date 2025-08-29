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
check_interval_seconds = 60 # Padrão para 1 minuto
periodic_task = None
WRAPPED_SOL_MINT_ADDRESS = "So11111111111111111111111111111111111111112"
parameters = {
    "base_token_symbol": None,
    "quote_token_symbol": None,
    "timeframe": None,
    "amount": None,
    "support_level": None,      # Nível de suporte
    "resistance_level": None,   # Nível de resistência
    "stop_loss_percent": None,  # % abaixo do suporte para o stop
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
    global in_position, entry_price
    details = parameters["trade_pair_details"]
    logger.info(f"EXECUTANDO ORDEM DE COMPRA REAL de {amount} {details['quote_symbol']} para {details['base_symbol']} ao preço de {price}")
    
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
            logger.warning("Tentativa de venda com saldo zero."); in_position = False; entry_price = 0.0; return
        tx_sig = await execute_swap(details['base_address'], details['quote_address'], amount_to_sell, token_decimals)
        if tx_sig:
            in_position = False
            entry_price = 0.0
            await send_telegram_message(f"🛑 VENDA REALIZADA: {amount_to_sell:.6f} de {details['base_symbol']}\nMotivo: {reason}\nhttps://solscan.io/tx/{tx_sig}")
        else:
            await send_telegram_message(f"❌ FALHA NA VENDA do token {details['base_symbol']}")
    except Exception as e:
        logger.error(f"Erro ao buscar saldo para venda: {e}"); await send_telegram_message(f"⚠️ Falha ao buscar saldo do token para venda: {e}")

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
                
                df.columns = [col.lower() for col in df.columns]

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
    global in_position, entry_price
    # Verifica se todos os parâmetros necessários foram definidos
    required_params = ["base_token_symbol", "quote_token_symbol", "timeframe", "amount", "support_level", "resistance_level", "stop_loss_percent"]
    if not bot_running or not all(parameters.get(p) is not None for p in required_params): return

    try:
        pair_details = parameters["trade_pair_details"]
        timeframe = parameters["timeframe"]
        amount = parameters["amount"]
        
        logger.info(f"Buscando dados de candles para {pair_details['base_symbol']}/{pair_details['quote_symbol']} no GeckoTerminal...")
        data = await fetch_geckoterminal_ohlcv(pair_details['pair_address'], timeframe)

        if data is None or data.empty:
            await send_telegram_message(f"⚠️ Não foi possível obter dados de velas do GeckoTerminal.")
            return
        
        logger.info(f"Recebidas {len(data)} velas do GeckoTerminal. Iniciando pré-processamento...")
        
        # Limpeza de dados
        data.replace([np.inf, -np.inf], np.nan, inplace=True)
        data.dropna(inplace=True)
        if len(data) < 2:
            logger.warning(f"Dados insuficientes ({len(data)} velas) após limpeza inicial.")
            return

        # --- CÁLCULO DOS INDICADORES PARA RANGE TRADING ---
        data.ta.rsi(length=14, append=True) # Adiciona a coluna 'RSI_14'
        data['volume_sma'] = data['volume'].rolling(window=20).mean()
        data.dropna(inplace=True)

        if data.empty or len(data) < 3:
            logger.warning("Não há dados suficientes após o período de aquecimento dos indicadores.")
            return

        current_candle = data.iloc[-2] # Usamos a penúltima vela (a última pode não estar fechada)
        
        # --- EXTRAÇÃO DE VALORES ATUAIS ---
        current_close = current_candle['close']
        current_volume = current_candle['volume']
        current_volume_sma = current_candle['volume_sma']
        current_rsi = current_candle['RSI_14']
        
        # --- LÓGICA DE RANGE TRADING ---
        support = parameters["support_level"]
        resistance = parameters["resistance_level"]
        stop_loss_percent = parameters["stop_loss_percent"]

        logger.info(
            f"Análise ({pair_details['base_symbol']}): "
            f"Preço {current_close:.8f} | "
            f"Vol {current_volume:.2f} | Média Vol {current_volume_sma:.2f} | "
            f"RSI {current_rsi:.2f} | "
            f"Suporte {support:.8f} | Resistência {resistance:.8f}"
        )

        if in_position:
            # O take profit é a resistência
            take_profit_price = resistance
            # O stop loss é calculado com base no nível de suporte, não no preço de entrada
            stop_loss_price = support * (1 - stop_loss_percent / 100)

            logger.info(f"Posição aberta. Entrada: {entry_price:.8f}, Take Profit: {take_profit_price:.8f}, Stop Loss: {stop_loss_price:.8f}")

            if current_close >= take_profit_price:
                await execute_sell_order(reason=f"Take Profit (Resistência) atingido em {take_profit_price:.8f}")
            elif current_close <= stop_loss_price:
                await execute_sell_order(reason=f"Stop Loss atingido em {stop_loss_price:.8f}")

        else: # Não está em posição, procurar por entrada
            # --- ALTERAÇÃO AQUI: Lógica da "Zona de Compra" ---
            # Define os limites da zona de compra
            buy_zone_upper_bound = support * 1.015  # 1.5% acima do suporte
            buy_zone_lower_bound = support * 0.995  # 0.5% abaixo do suporte (mais agressivo)

            # 1. Preço está dentro da zona de compra
            price_in_buy_zone = buy_zone_lower_bound <= current_close <= buy_zone_upper_bound
            
            # 2. RSI está em território de sobrevenda
            rsi_oversold = current_rsi < 35 # Usar 35 para ser um pouco menos restritivo que 30
            
            # 3. Confirmação de volume
            volume_confirmation = current_volume > current_volume_sma # Pelo menos acima da média

            if price_in_buy_zone and rsi_oversold and volume_confirmation:
                logger.info(f"Sinal de COMPRA (Range Trading): Preço na zona de compra ({current_close:.8f}), RSI em sobrevenda ({current_rsi:.2f}) e volume confirmado.")
                await execute_buy_order(amount, current_close)

    except Exception as e:
        logger.error(f"Ocorreu um erro em check_strategy: {e}", exc_info=True)
        await send_telegram_message(f"⚠️ Erro inesperado ao executar a estratégia: {e}")


async def send_telegram_message(message):
    if application:
        await application.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')

async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Sou seu bot de autotrade para a rede Solana.\n'
        'Estratégia: **Range Trading com Confirmação de RSI**.\n'
        'Fonte de Dados: **GeckoTerminal**.\n\n'
        'Use o comando `/set` para configurar:\n'
        '`/set <CONTRATO> <COTAÇÃO> <TIMEFRAME> <VALOR> <SUPORTE> <RESISTENCIA> <STOP_LOSS_%>`\n\n'
        '**Exemplo (PENGU/SOL - Range de Curto Prazo):**\n'
        '`/set 67dmC6iG5sAh4xQdEe4A2t4gYg3sM24g1vQdY8fJzK4g SOL 1m 0.1 0.0292 0.0298 1.5`\n\n'
        '**O que os parâmetros significam:**\n'
        '- **CONTRATO:** Endereço do token base (ex: PENGU).\n'
        '- **COTAÇÃO:** Moeda de cotação (ex: SOL).\n'
        '- **TIMEFRAME:** `1m`, `5m`, `15m`, `1h`, `4h`, `1d`.\n'
        '- **VALOR:** Quantidade a comprar (em SOL).\n'
        '- **SUPORTE:** Preço do nível de suporte.\n'
        '- **RESISTENCIA:** Preço do nível de resistência (será o take-profit).\n'
        '- **STOP_LOSS_%:** Percentual abaixo do suporte para o stop loss (ex: `1.5` para 1.5%).\n\n'
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
        # Novos parâmetros para Range Trading
        base_token_contract = context.args[0]
        quote_symbol_input = context.args[1].upper()
        timeframe = context.args[2].lower()
        amount = float(context.args[3])
        support_level = float(context.args[4])
        resistance_level = float(context.args[5])
        stop_loss_percent = float(context.args[6])

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
            "amount": amount,
            "support_level": support_level,
            "resistance_level": resistance_level,
            "stop_loss_percent": stop_loss_percent,
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
            f"📈 *Estratégia:* Range Trading com RSI\n"
            f"💰 *Valor por Ordem:* `{amount}` {quote_symbol_input}\n"
            f"🔵 *Suporte:* `{support_level:.8f}`\n"
            f"🔴 *Resistência (Take Profit):* `{resistance_level:.8f}`\n"
            f"📉 *Stop Loss:* `{stop_loss_percent}%` abaixo do suporte",
            parse_mode='Markdown'
        )
    except (IndexError, ValueError):
        await update.effective_message.reply_text(
            "⚠️ *Erro: Formato incorreto.*\n"
            "Use: `/set <CONTRATO> <COTAÇÃO> <TIMEFRAME> <VALOR> <SUPORTE> <RESISTENCIA> <STOP_LOSS_%>`\n"
            "Exemplo: `/set ... SOL 1m 0.1 0.0292 0.0298 1.5`",
            parse_mode='Markdown'
        )
    except httpx.HTTPStatusError as e:
        await update.effective_message.reply_text(f"⚠️ Erro ao comunicar com a API do Dexscreener: {e}")
    except Exception as e:
        logger.error(f"Erro inesperado em set_params: {e}")
        await update.effective_message.reply_text(f"⚠️ Ocorreu um erro ao configurar os parâmetros: {e}")

async def run_bot(update, context):
    global bot_running, periodic_task
    required_params = ["base_token_symbol", "quote_token_symbol", "timeframe", "amount", "support_level", "resistance_level", "stop_loss_percent"]
    if not all(parameters.get(p) is not None for p in required_params):
        await update.effective_message.reply_text("Defina os parâmetros com /set primeiro.")
        return
    if bot_running:
        await update.effective_message.reply_text("O bot já está em execução.")
        return
    
    bot_running = True
    logger.info("Bot de trade iniciado.")
    await update.effective_message.reply_text("🚀 Bot iniciado! Verificando a estratégia de Range Trading via GeckoTerminal...")
    
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
