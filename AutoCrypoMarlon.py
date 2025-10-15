# sniper_bot.py
import os
import time
import logging
import asyncio
from datetime import datetime, timezone
from base64 import b64decode
import httpx
import pandas as pd
from collections import Counter

from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned

from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from spl.token.instructions import get_associated_token_address

from telegram.ext import Application, CommandHandler
import telegram

from flask import Flask
from threading import Thread

# --- CÓDIGO DO SERVIDOR WEB ---
app = Flask('')
@app.route('/')
def home():
    return "Bot is alive!"
def run_server():
  app.run(host='0.0.0.0',port=8000)
def keep_alive():
    t = Thread(target=run_server)
    t.start()
# --- FIM DO CÓDIGO DO SERVIDOR ---

# ---------------- Configuração ----------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("sniper_bot")

# Variáveis de ambiente (Koyeb)
# --- Configurações Iniciais ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
PRIVATE_KEY_B58 = os.getenv("PRIVATE_KEY_BASE58")
RPC_URL = os.getenv("RPC_URL")

MIN_LIQUIDITY = 40000  # Mínimo de $40,000 de liquidez
MIN_VOLUME_H1 = 100000 # Mínimo de $100,000 de volume na última hora
TRADE_INTERVAL_SECONDS = 30

if not all([RPC_URL, PRIVATE_KEY_B58, TELEGRAM_TOKEN, CHAT_ID]):
    logger.error("Erro: variáveis de ambiente RPC_URL, PRIVATE_KEY_B58, TELEGRAM_TOKEN e CHAT_ID são obrigatórias.")
    raise SystemExit(1)

try:
    CHAT_ID = int(CHAT_ID)
except Exception:
    logger.error("CHAT_ID deve ser um inteiro (ID do chat).")
    raise SystemExit(1)

# Solana client (sync) e payer (a partir do Base58 private key que você informou)
solana_client = Client(RPC_URL)
try:
    payer = Keypair.from_base58_string(PRIVATE_KEY_B58)
    logger.info(f"Carteira carregada. Pubkey: {payer.pubkey()}")
except Exception as e:
    logger.error(f"Erro ao carregar private key: {e}")
    raise

# ---------------- Estado global ----------------
application = None

bot_running = False
periodic_task = None

in_position = False
entry_price = 0.0 # Initialize entry_price

sell_fail_count = 0
buy_fail_count = 0

automation_state = {
    "current_target_pair_address": None,
    "current_target_symbol": None,
    "current_target_pair_details": None,
    "last_scan_timestamp": 0,
    "position_opened_timestamp": 0,
    "target_selected_timestamp": 0,
    "penalty_box": {},
    "discovered_pairs": {},
    "took_profit_pairs": set(),
    "checking_volatility": False,
    "volatility_check_start_time": 0
}

parameters = {
    "timeframe": "1m",
    "amount": None,                 # em SOL
    "stop_loss_percent": None,      # ex: 15
    "take_profit_percent": None,    # ex: 20
    "priority_fee": 2000000
}

# ---------------- Utilitários Telegram ----------------
async def send_telegram_message(message):
    """Envia mensagem para o chat configurado (usa application global)."""
    if application:
        try:
            await application.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')
        except telegram.error.RetryAfter as e:
            logger.warning(f"Telegram flood control: aguardando {e.retry_after}s")
            await asyncio.sleep(e.retry_after)
            try:
                await application.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')
            except Exception as e2:
                logger.error(f"Falha ao reenviar mensagem ao Telegram: {e2}")
        except Exception as e:
            logger.error(f"Erro ao enviar mensagem para Telegram: {e}")
    else:
        logger.info("application não inicializado; mensagem Telegram não enviada.")

# ---------------- Funções de swap / slippage (seu código) ----------------
async def execute_swap(input_mint_str, output_mint_str, amount, input_decimals, slippage_bps):
    logger.info(f"Iniciando swap de {amount} do token {input_mint_str} para {output_mint_str} com slippage de {slippage_bps} BPS e limite computacional dinâmico.")
    amount_wei = int(amount * (10**input_decimals))

    max_retries = 5
    for attempt in range(max_retries):
        async with httpx.AsyncClient() as client:
            try:
                # Atualizado para o novo endpoint da Jupiter
                quote_url = f"https://lite-api.jup.ag/swap/v1/quote?inputMint={input_mint_str}&outputMint={output_mint_str}&amount={amount_wei}&slippageBps={slippage_bps}&maxAccounts=64"
                quote_res = await client.get(quote_url, timeout=60.0)
                quote_res.raise_for_status()
                quote_response = quote_res.json()

                swap_payload = {
                    "userPublicKey": str(payer.pubkey()),
                    "quoteResponse": quote_response,
                    "wrapAndUnwrapSol": True,
                    "dynamicComputeUnitLimit": True,
                    "prioritizationFeeLamports": {
                        "priorityLevelWithMaxLamports": {
                            "maxLamports": 10000000,
                            "priorityLevel": "veryHigh"
                        }
                    }
                }

                # Atualizado para o novo endpoint da Jupiter
                swap_url = "https://lite-api.jup.ag/swap/v1/swap"
                swap_res = await client.post(swap_url, json=swap_payload, timeout=60.0)
                swap_res.raise_for_status()
                swap_response = swap_res.json()
                swap_tx_b64 = swap_response.get('swapTransaction')
                if not swap_tx_b64:
                    logger.error(f"Erro na API da Jupiter: {swap_response}"); return None

                raw_tx_bytes = b64decode(swap_tx_b64)
                swap_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
                # assinatura com solders - use to_bytes_versioned
                signature = payer.sign_message(to_bytes_versioned(swap_tx.message))
                signed_tx = VersionedTransaction.populate(swap_tx.message, [signature])

                tx_opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
                # send_raw_transaction espera bytes
                tx_signature = solana_client.send_raw_transaction(bytes(signed_tx), opts=tx_opts).value

                logger.info(f"Transação enviada: {tx_signature}")

                # tentativa de confirmação
                try:
                    solana_client.confirm_transaction(tx_signature, commitment="confirmed")
                    logger.info(f"Transação confirmada: https://solscan.io/tx/{tx_signature}")
                    return str(tx_signature) # Success
                except Exception as confirm_e:
                    logger.error(f"Transação enviada, mas falha na confirmação: {confirm_e}")
                    # Confirmation failed, but transaction might still go through.
                    # For simplicity here, we treat this as a failure to trigger retry or main loop retry.
                    pass # Let the outer loop handle retries if tx_sig is None

            except Exception as e:
                logger.error(f"Falha na transação (tentativa {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(5) # Wait before retrying

    logger.error(f"Falha na transação após {max_retries} tentativas.")
    await send_telegram_message(f"⚠️ Falha na transação após {max_retries} tentativas: {e}")
    return None # Return None if all retries fail


async def calculate_dynamic_slippage(pair_address):
    logger.info(f"Calculando slippage dinâmico para {pair_address} com base na volatilidade...")
    df = await fetch_geckoterminal_ohlcv(pair_address, "1m", limit=5)
    if df is None or df.empty or len(df) < 5:
        logger.warning("Dados insuficientes. Usando slippage padrão (5.0%).")
        return 500, 0 # Return default slippage and 0 volatility
    price_range = df['high'].max() - df['low'].min()
    volatility = (price_range / df['low'].min()) * 100 if df['low'].min() > 0 else 0
    if volatility > 10.0:
        slippage_bps = 500 # 5%
    else:
        slippage_bps = 200  # 2%
    logger.info(f"Volatilidade ({volatility:.2f}%). Slippage definido para {slippage_bps/100:.2f}%.")
    return slippage_bps, volatility # Return slippage and volatility

# ---------------- Funções de dados reais (Dexscreener / Geckoterminal) ----------------
async def get_pair_details(pair_address):
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            res.raise_for_status()

            # A API retorna um array de pares, mesmo que você procure por um só
            pair_data = res.json().get('pairs', [None])[0]
            if not pair_data:
                return None

            # Retorna o dicionário completo que a função analyze_and_score_coin espera
            # Extraímos todos os dados necessários aqui
            return pair_data

    except Exception as e:
        # Se a requisição falhar, a função retorna None, o que já é tratado no loop
        return None

async def fetch_geckoterminal_ohlcv(pair_address, timeframe, limit=60):
    # timeframe map (apenas 1m implementado)
    timeframe_map = {"1m": "minute", "5m": "minute"}  # para 5m tratamos agregando candles mais tarde se necessário
    gt_timeframe = timeframe_map.get(timeframe, "minute")
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair_address}/ohlcv/{gt_timeframe}?aggregate=1&limit={limit}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            res.raise_for_status()
            data = res.json()
            if data.get('data') and data['data'].get('attributes', {}).get('ohlcv_list'):
                ohlcv = data['data']['attributes']['ohlcv_list']
                df = pd.DataFrame(ohlcv, columns=['ts','o','h','l','c','v'])
                df[['o','h','l','c','v']] = df[['o','h','l','c','v']].apply(pd.to_numeric)
                df.rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'}, inplace=True)
                return df.tail(limit).reset_index(drop=True)
    except Exception:
        return None

async def fetch_dexscreener_real_time_price(pair_address):
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=5.0)
            res.raise_for_status()
            pair_data = res.json().get('pair')
            if pair_data:
                # priceNative and priceUsd available depending on the pair
                native = pair_data.get('priceNative')
                usd = pair_data.get('priceUsd')
                try:
                    return float(native if native is not None else 0), float(usd if usd is not None else 0)
                except:
                    return None, None
            return None, None
    except Exception:
        return None, None

async def is_pair_quotable_on_jupiter(pair_details):
    if not pair_details: return False
    test_amount_wei = 10000
    # Atualizado para o novo endpoint da Jupiter
    url = f"https://lite-api.jup.ag/swap/v1/quote?inputMint={pair_details['quoteToken']['address']}&outputMint={pair_details['baseToken']['address']}&amount={test_amount_wei}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            return res.status_code == 200
    except Exception:
        return False

# ---------------- Descoberta e Filtragem (implementado conforme solicitado) ----------------
async def get_new_pools(page=1):
    """Busca uma página de novas pools da API GeckoTerminal."""
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools?page={page}&sort=h24_volume_usd_desc&include=base_token,quote_token"
    headers = {'Accept': 'application/json'}
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, headers=headers, timeout=10.0)
            resp.raise_for_status() # Lança um erro para respostas 4xx/5xx
            data = resp.json()
            return data.get('data', [])
        except httpx.HTTPStatusError as e:
            logger.error(f"Erro de status da API ao buscar pools: {e.response.status_code} - {e.response.text}")
        except httpx.RequestError as e:
            logger.error(f"Erro de requisição ao buscar pools: {e}")
        except Exception as e:
            logger.error(f"Erro inesperado ao buscar pools: {e}")
        return []

import time
import requests
from datetime import datetime, timedelta

import time
import requests
from datetime import datetime

async def discover_and_filter_pairs(pages_to_scan=1):
    print(f"Iniciando a busca por moedas em tendência na rede Solana, escaneando {pages_to_scan} página(s)...") # Updated message

    filtered_pairs = []

    for page in range(1, pages_to_scan + 1):
        # Updated API endpoint to trending_pools
        url = f"https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?page={page}"
        headers = {'Content-Type': 'application/json'}

        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

            gecko_pairs = data.get('data', [])

            if not gecko_pairs:
                print(f"Nenhum par em tendência encontrado na página {page}. Encerrando a busca...") # Updated message
                break

            for gecko_pair in gecko_pairs:
                attributes = gecko_pair['attributes']

                # Nome e endereço do par para logs
                pair_name = attributes.get('name', 'N/A')
                pair_address = attributes['address']

                # --- LÓGICA DE FILTRAGEM (Ajustada para trending_pools) ---
                # trending_pools não tem 'pool_created_at', 'reserve_in_usd', 'transactions' na mesma estrutura que new_pools
                # Precisaremos buscar detalhes adicionais ou usar critérios diferentes
                # Por enquanto, vamos apenas buscar os detalhes na Dexscreener para cada par em tendência encontrado
                dex_pair_details = await get_pair_details(pair_address)

                if dex_pair_details:
                     # Adding basic filters from the old logic, adapted for Dexscreener data
                    try:
                        liquidity_usd = float(dex_pair_details.get('liquidity', {}).get('usd', 0))
                        txns_h1 = dex_pair_details.get('txns', {}).get('h1', {'buys': 0, 'sells': 0})
                        txns_h1_buys = txns_h1.get('buys', 0)
                        txns_h1_sells = txns_h1.get('sells', 0)
                        volume_h1_usd = float(dex_pair_details.get('volume', {}).get('h1', 0))
                        price_change_h1 = float(dex_pair_details.get('priceChange', {}).get('h1', 0))

                        logger.info(f"Avalianco par {pair_name} ({pair_address}): Liquidez: ${liquidity_usd:,.2f}, Volume H1: ${volume_h1_usd:,.2f}, Compras H1: {txns_h1_buys}, Vendas H1: {txns_h1_sells}, Variação H1: {price_change_h1:.2f}%")


                        # Check if the pair is older than 1 hour
                        created_at = dex_pair_details.get('pairCreatedAt')
                        if created_at:
                            # Convert milliseconds to seconds
                            created_at_timestamp = created_at / 1000
                            current_timestamp = time.time()
                            age_in_seconds = current_timestamp - created_at_timestamp
                            if age_in_seconds < 3600:  # 3600 seconds = 1 hour
                                logger.info(f"❌ Par {pair_name} ({pair_address}) eliminado: Muito novo ({age_in_seconds:.0f}s).")
                                continue
                        else:
                             logger.info(f"⚠️ Não foi possível obter o timestamp de criação para {pair_name} ({pair_address}). Pulando filtro de idade.")


                        if liquidity_usd < 50000:
                             logger.info(f"❌ Par {pair_name} ({pair_address}) eliminado: Baixa liquidez (USD: {liquidity_usd:,.2f}).")
                             continue

                        # Updated condition to 0.9
                        if txns_h1_buys > 0 and (txns_h1_sells / txns_h1_buys) > 0.9:
                            logger.info(f"❌ Par {pair_name} ({pair_address}) eliminado: Proporção de vendas muito alta ({txns_h1_sells} vendas, {txns_h1_buys} compras).")
                            continue

                        if volume_h1_usd < 100000: # Removed price_change_h1 filter
                            logger.info(f"❌ Par {pair_name} ({pair_address}) eliminado: Volume insuficiente (Volume: {volume_h1_usd:,.2f} USD).")
                            continue


                        filtered_pairs.append(dex_pair_details)
                        print(f"✅ Moeda em tendência encontrada e validada na Solana: {dex_pair_details['baseToken']['symbol']} - Endereço: {pair_address}")
                    except (KeyError, TypeError, ValueError) as e:
                         print(f"❌ Par {pair_name} ({pair_address}) eliminado: Erro ao processar dados da Dexscreener: {e}")
                         continue

                else:
                    print(f"⚠️ Par {pair_name} ({pair_address}) eliminado: Não encontrado na Dexscreener ou dados insuficientes.")


        except requests.exceptions.RequestException as e:
            print(f"Erro na requisição para a API do GeckoTerminal (Página {page}): {e}")
            break

    return filtered_pairs

def analyze_and_score_coin(pair_details):
    """
    Analisa e pontua uma moeda com base em dados de volume, preço e transações.
    A função é agora compatível com a estrutura de dados da Dexscreener.
    """

    try:
        # Extraindo dados para a análise da estrutura de dados da Dexscreener
        # Usamos o método .get() para evitar erros caso a chave não exista
        volume_h1_usd = float(pair_details.get('volume', {}).get('h1', 0))
        price_change_h1 = float(pair_details.get('priceChange', {}).get('h1', 0))

        txns_h1 = pair_details.get('txns', {}).get('h1', {'buys': 0, 'sells': 0})
        txns_h1_buys = txns_h1.get('buys', 0)
        txns_h1_sells = txns_h1.get('sells', 0)

        # --- Lógica de Pontuação ---
        # Pontuação 1: Volume
        volume_score = 0
        if volume_h1_usd >= 300000:
            volume_score = 40
        elif volume_h1_usd >= 100000:
            volume_score = 30
        elif volume_h1_usd >= 50000:
            volume_score = 15

        # Pontuação 2: Variação de Preço
        price_change_score = 0
        if price_change_h1 >= 500:
            price_change_score = -30 # Adjusted to 0 for >= 500% change
        elif price_change_h1 >= 100:
            price_change_score = 20
        elif price_change_h1 >= 30:
            price_change_score = 10
        elif price_change_h1 >= 5:
             price_change_score = 5 

        # Pontuação 3: Compras vs. Vendas
        buys_sells_score = 0
        buy_ratio = 0 # Initialize buy_ratio outside the if block

        if txns_h1_sells >= 100: # Check for minimum sell transactions
            if txns_h1_buys > 0:
                buy_ratio = txns_h1_buys / (txns_h1_buys + txns_h1_sells)
                if buy_ratio >= 0.8: # 90% ou mais de compras
                    buys_sells_score = 30
                elif buy_ratio >= 0.7: # 80% ou mais de compras
                    buys_sells_score = 20
                elif buy_ratio >= 0.5: # 50% ou mais de compras
                    buys_sells_score = 10
            # If txns_h1_buys is 0 but txns_h1_sells >= 100, buy_ratio remains 0 and score is 0, which is correct.
        else:
            print(f"❌ Par {pair_details.get('baseToken', {}).get('symbol', 'N/A')} eliminado na pontuação: Menos de 100 vendas em 1h ({txns_h1_sells}).") # Log when excluded by this rule
            buys_sells_score = 0 # Set score to 0 if minimum sells not met

        # Calculando a pontuação final
        final_score = volume_score + price_change_score + buys_sells_score

        print(f"Análise de {pair_details['baseToken']['symbol']}:")
        print(f"  Volume (H1): ${volume_h1_usd:,.2f} -> Pontos: {volume_score}")
        print(f"  Variação (H1): {price_change_h1:.2f}% -> Pontos: {price_change_score}")
        print(f"  Compras/Vendas: {buy_ratio:.2f} -> Pontos: {buys_sells_score}")
        print(f"  Pontuação Final: {final_score:.2f}\n")

        return final_score

    except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
        print(f"Erro ao analisar a moeda {pair_details.get('baseToken', {}).get('symbol', 'N/A')}: {e}")
        return 0

async def find_best_coin_to_trade(pair_info):
    """
    Analisa uma única moeda aprovada, calcula sua pontuação e retorna seus detalhes.
    """
    if not pair_info or 'pairAddress' not in pair_info:
        logger.warning("Nenhuma informação de par válida recebida para análise.")
        return None, {}

    address = pair_info.get('pairAddress')

    logger.info(f"🔎 Analisando o par aprovado: {address})")

    # Fetch complete details from Dexscreener
    details = await get_pair_details(address)

    if not details:
        logger.error(f"Não foi possível obter os detalhes completos para o par no endereço {address}")
        return None, {}

    symbol = details.get('baseToken', {}).get('symbol', 'N/A')
    logger.info(f"🔎 Analisando o par aprovado: {symbol} ({address})")

    # CORREÇÃO: Chamando a função de pontuação com o nome correto ('analyze_and_score_coin')
    # e passando os argumentos que ela espera (symbol, address).
    score = analyze_and_score_coin(details) # Pass the full details dictionary

    # A função original retorna 0 em caso de erro.
    if score is None or score == 0:
        logger.error(f"Não foi possível calcular a pontuação para {symbol}. Descartando.")
        return None, {}

    logger.info(f"🏆 Par único analisado: {symbol} (Score={score:.2f})")

    try:
        details['score'] = score
        best_pair_symbol = details.get('baseToken', {}).get('symbol', symbol)

        return best_pair_symbol, details

    except Exception as e:
        logger.error(f"Erro crítico ao obter detalhes finais para {symbol}: {e}", exc_info=True)
        return None, {}


# ---------------- Ordem: BUY / SELL (usando seu código) ----------------
async def execute_buy_order(amount, price, pair_details, manual=False, reason="Sinal da Estratégia"):
    global in_position, entry_price, sell_fail_count, buy_fail_count

    if in_position:
        logger.info("Já em posição, abortando compra.")
        return

    if not manual:
        logger.info(f"Verificação final de cotação para {pair_details['baseToken']['symbol']} antes da compra...")
        # Add retry logic for Jupiter quotability check
        retries = 3
        is_quotable = False
        for attempt in range(retries):
            if await is_pair_quotable_on_jupiter(pair_details):
                is_quotable = True
                break
            logger.warning(f"Par {pair_details['baseToken']['symbol']} não quotable na Jupiter. Tentativa {attempt + 1}/{retries}. Aguardando 5 segundos...")
            await asyncio.sleep(5)

        if not is_quotable:
            logger.error(f"FALHA NA COMPRA: Par {pair_details['baseToken']['symbol']} não se tornou negociável na Jupiter após {retries} tentativas. Penalizando e buscando novo alvo.")
            await send_telegram_message(f"❌ Compra para **{pair_details['baseToken']['symbol']}** abortada. Moeda não negociável na Jupiter após várias tentativas.")
            if automation_state.get("current_target_pair_address"):
                automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                automation_state["current_target_pair_address"] = None
            return

    # Calculate slippage and get volatility
    slippage_bps, volatility = await calculate_dynamic_slippage(pair_details['pairAddress'])

    # Check volatility before executing the swap
    if volatility > 80:
        logger.warning(f"Alta volatilidade ({volatility:.2f}%) para {pair_details['baseToken']['symbol']}. Abortando compra.")
        await send_telegram_message(f"⚠️ Compra para **{pair_details['baseToken']['symbol']}** abortada devido à alta volatilidade ({volatility:.2f}%).")
        if automation_state.get("current_target_pair_address"):
             automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 60 # Penalize for high volatility
             await send_telegram_message(f"⚠️ **{pair_details['baseToken']['symbol']}** penalizada por 60 ciclos devido à alta volatilidade.")

        automation_state["current_target_pair_address"] = None # Reset target
        return # Exit without executing swap


    logger.info(f"EXECUTANDO ORDEM DE COMPRA de {amount} SOL para {pair_details['baseToken']['symbol']} ao preço de {price}")

    tx_sig = await execute_swap(pair_details['quoteToken']['address'], pair_details['baseToken']['address'], amount, 9, slippage_bps)

    if tx_sig:
        in_position = True
        entry_price = price # Set entry_price on successful buy (in USD)
        automation_state["position_opened_timestamp"] = time.time()
        sell_fail_count = 0
        buy_fail_count = 0
        log_message = (f"✅ COMPRA REALIZADA: {amount} SOL para {pair_details['baseToken']['symbol']}\n"
                       f"Motivo: {reason}\n"
                       f"Entrada (USD): ${price:.10f} | Alvo (USD): ${price * (1 + parameters['take_profit_percent']/100):.10f} | "
                       f"Stop (USD): ${price * (1 - parameters['stop_loss_percent']/100):.10f}\n"
                       f"Slippage Usado: {slippage_bps/100:.2f}%\n"
                       f"Taxa de Prioridade: {parameters.get('priority_fee')} micro-lamports\n"
                       f"https://solscan.io/tx/{tx_sig}")
        logger.info(log_message)
        await send_telegram_message(log_message)
    else:
        buy_fail_count += 1
        logger.error(f"FALHA NA EXECUÇÃO da compra para {pair_details['baseToken']['symbol']}. Tentativa {buy_fail_count}/10.")
        if buy_fail_count >= 10:
            logger.error(f"Limite de falhas de compra atingido para {pair_details['baseToken']['symbol']}. Penalizando.")
            await send_telegram_message(f"❌ FALHA NA EXECUÇÃO da compra para **{pair_details['baseToken']['symbol']}**. Limite atingido. Moeda penalizada.")
            if automation_state.get("current_target_pair_address"):
                automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                automation_state["current_target_pair_address"] = None
            buy_fail_count = 0

async def execute_sell_order(reason="", sell_price=None):
    global in_position, entry_price, sell_fail_count, buy_fail_count
    if not in_position: return

    pair_details = automation_state.get('current_target_pair_details', {})
    symbol = pair_details.get('baseToken', {}).get('symbol', 'TOKEN')
    pair_address = pair_details.get('pairAddress')

    logger.info(f"EXECUTANDO ORDEM DE VENDA de {symbol}. Motivo: {reason}")
    try:
        token_mint_pubkey = Pubkey.from_string(pair_details['baseToken']['address'])
        ata_address = get_associated_token_address(payer.pubkey(), token_mint_pubkey)

        balance_response = solana_client.get_token_account_balance(ata_address)

        if hasattr(balance_response, 'value'):
            token_balance_data = balance_response.value
        else:
            logger.error(f"Erro ao obter saldo do token {symbol}: Resposta RPC inválida.")
            # Increment fail count and notify, but don't abandon position yet
            sell_fail_count += 1
            await send_telegram_message(f"⚠️ Erro ao obter saldo do token {symbol}. A venda falhou. Tentativa {sell_fail_count}/100. O bot tentará novamente.")
            return # Return without changing position state

        amount_to_sell = token_balance_data.ui_amount
        if amount_to_sell is None or amount_to_sell == 0:
            logger.warning("Tentativa de venda com saldo zero, resetando posição.")
            # Reset position state as there's nothing to sell
            in_position = False
            entry_price = 0.0
            automation_state["position_opened_timestamp"] = 0
            automation_state["current_target_pair_address"] = None
            sell_fail_count = 0
            return

        slippage_bps, _ = await calculate_dynamic_slippage(pair_details['pairAddress']) # Volatility not needed for sell slippage
        tx_sig = await execute_swap(pair_details['baseToken']['address'], pair_details['quoteToken']['address'], amount_to_sell, token_balance_data.decimals, slippage_bps)

        if tx_sig:
            # Calculate P/L using entry_price (USD) and sell_price (USD)
            profit_loss_percent = 0.0
            if entry_price is not None and entry_price > 0 and sell_price is not None:
                 profit_loss_percent = ((sell_price - entry_price) / entry_price) * 100

            log_message = (f"🛑 VENDA REALIZADA: {symbol}\n"
                           f"Motivo: {reason}\n"
                           f"Lucro/Prejuízo: {profit_loss_percent:.2f}%\n"
                           f"Entrada (USD): ${entry_price:.10f} | Saída (USD): ${sell_price:.10f}\n" # Corrected display
                           f"Slippage Usado: {slippage_bps/100:.2f}%\n"
                           f"Taxa de Prioridade: {parameters.get('priority_fee')} micro-lamports\n"
                           f"https://solscan.io/tx/{tx_sig}")
            logger.info(log_message)
            await send_telegram_message(log_message)

            # Successfully sold, reset position and fail counts
            in_position = False
            entry_price = 0.0
            automation_state["position_opened_timestamp"] = 0

            # Apply penalty based on reason
            if pair_address:
                if "Take Profit" in reason:
                    automation_state["penalty_box"][pair_address] = 100 # Increased penalty for TP
                    await send_telegram_message(f"💰 **{symbol}** atingiu o Take Profit e foi penalizada por 100 ciclos.")
                elif "Stop Loss" in reason:
                     automation_state["penalty_box"][pair_address] = 100 # Standard penalty for SL
                     await send_telegram_message(f"⚠️ **{symbol}** atingiu o Stop Loss e foi penalizada por 100 ciclos.")
                elif "Timeout" in reason:
                    automation_state["penalty_box"][pair_address] = 100 # Standard penalty for Timeout
                    await send_telegram_message(f"⏰ **{symbol}** atingiu o Timeout e foi penalizada por 100 ciclos.")
                elif "Parada manual" in reason:
                    automation_state["penalty_box"][pair_address] = 10 # Lower penalty for Manual Stop
                    await send_telegram_message(f"✋ **{symbol}** foi vendida manualmente e penalizada por 10 ciclos.")

                automation_state["current_target_pair_address"] = None # Reset target after selling


            sell_fail_count = 0
            buy_fail_count = 0
        else:
            # This block is reached if execute_swap returns None after retries
            logger.error(f"FALHA NA VENDA do token {symbol} após retentativas. Tentativa {sell_fail_count+1}/100.")
            sell_fail_count += 1
            await send_telegram_message(f"❌ FALHA NA VENDA do token {symbol} após retentativas. Tentativa {sell_fail_count}/100. O bot tentará novamente.")

            if sell_fail_count >= 100: # Check limit AFTER incrementing
                logger.error(f"ATINGIDO LIMITE DE {sell_fail_count} FALHAS DE VENDA. RESETANDO POSIÇÃO.")
                await send_telegram_message(f"⚠️ Limite de {sell_fail_count} falhas de venda para **{symbol}** atingido. Posição abandonada.")
                # Abandon position and penalize on hitting the limit
                in_position = False
                entry_price = 0.0
                automation_state["position_opened_timestamp"] = 0
                if pair_address:
                    automation_state["penalty_box"][pair_address] = 100 # Penalize heavily on final failure
                    await send_telegram_message(f"⚠️ **{symbol}** foi penalizada por 100 ciclos após {sell_fail_count} falhas de venda.")
                automation_state["current_target_pair_address"] = None
                sell_fail_count = 0 # Reset fail count after abandoning

    except Exception as e:
        logger.error(f"Erro crítico ao vender {symbol}: {e}")
        # Increment sell_fail_count for unhandled exceptions in manage_position
        sell_fail_count += 1
        await send_telegram_message(f"⚠️ Erro em manage_position para {symbol}: {e}. Tentativa {sell_fail_count}/100. O bot permanecerá em posição.")
        if sell_fail_count >= 100: # Check limit AFTER incrementing
             logger.error(f"ATINGIDO LIMITE DE {sell_fail_count} ERROS EM manage_position. RESETANDO POSIÇÃO.")
             await send_telegram_message(f"⚠️ Limite de {sell_fail_count} erros em manage_position para **{symbol}** atingido. Posição abandonada.")
             in_position = False
             entry_price = 0.0
             automation_state["position_opened_timestamp"] = 0
             if automation_state.get('current_target_pair_address'):
                 automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 100 # Penalize heavily on final failure
                 await send_telegram_message(f"⚠️ **{symbol}** foi penalizada por 100 ciclos após {sell_fail_count} erros em manage_position.")
             automation_state["current_target_pair_address"] = None
             sell_fail_count = 0 # Reset fail count after abandoning


# ---------------- Estratégia velocity / momentum & adaptive timeout ----------------
import time
import asyncio

async def check_velocity_strategy():
    """Analisa o alvo atual para um gatilho de compra imediato e executa a ordem."""
    global in_position, automation_state
    pair_details = automation_state.get("current_target_pair_details")

    if not pair_details or in_position:
        return

    target_address = pair_details.get('pairAddress')
    symbol = pair_details.get('baseToken', {}).get('symbol', 'N/A')
    target_selected_timestamp = automation_state.get("target_selected_timestamp", 0)

    # Check if monitoring time exceeds 5 minutes (300 seconds)
    now = time.time()
    if now - target_selected_timestamp > 300:
        logger.info(f"Monitoramento do alvo {symbol} ({target_address}) excedeu 5 minutos. Abandonando alvo e retornando à caça.")
        await send_telegram_message(f"⏰ Monitoramento para **{symbol}** ({target_address}) excedeu o limite de 5 minutos. Abandonando alvo.")
        # Penalize the coin for failing to trigger a buy within the time limit
        if target_address:
             automation_state["penalty_box"][target_address] = 60 # Penalize for 1 minute (adjust as needed)
             await send_telegram_message(f"⚠️ **{symbol}** penalizada por 60 ciclos por não atingir gatilho de compra.")

        automation_state["current_target_pair_address"] = None
        automation_state["current_target_symbol"] = None
        automation_state["current_target_pair_details"] = None
        automation_state["target_selected_timestamp"] = 0 # Reset timestamp
        return # Exit the function to go back to hunting state


    try:
        # Puxa os dados das últimas 10 velas de 1 minuto para calcular a EMA de 10
        df_1m = await fetch_geckoterminal_ohlcv(target_address, "1m", limit=10)

        if df_1m is None or df_1m.empty or len(df_1m) < 10: # Need at least 10 candles for EMA 10
            logger.warning(f"Dados OHLCV insuficientes para {symbol} (necessário 10 velas para EMA 10). Tentando novamente.")
            return

        # Calculate EMA 10
        df_1m['EMA10'] = df_1m['close'].ewm(span=10, adjust=False).mean()

        # Get the current price (closing price of the last candle)
        current_price = df_1m['close'].iloc[-1]
        ema10 = df_1m['EMA10'].iloc[-1]

        # Log the analysis for visibility
        logger.info(f"🕵️ Monitorando {symbol}: Preço Atual: {current_price:.8f} | EMA 10: {ema10:.8f}")

        # New buy condition: Price > EMA 10
        if current_price > ema10:
            # Obtém o preço atual em USD para usar na compra
            _, price_usd = await fetch_dexscreener_real_time_price(target_address)

            if price_usd is None or price_usd == 0:
                 logger.warning(f"Não foi possível obter o preço atual em USD para {symbol}. Não é possível comprar.")
                 return

            price = price_usd # Use USD price for entry_price
            reason = "Gatilho: Preço acima da EMA 10 (1m)"

            msg = f"✅ GATILHO ATINGIDO para **{symbol}**! Preço ({current_price:.8f}) acima da EMA 10 ({ema10:.8f}). Executando ordem de compra..."
            logger.info(msg.replace("**",""))
            await send_telegram_message(msg)

            # Chama a função de compra com todos os argumentos necessários
            await execute_buy_order(parameters["amount"], price, pair_details, reason=reason)

        else:
            logger.info(f"❌ Condição de compra (Preço > EMA 10) NÃO atendida para {symbol}. Continuará monitorando.")

    except Exception as e:
        logger.error(f"Erro em check_velocity_strategy: {e}", exc_info=True)


async def manage_position():
    """Gerencia a posição de trade, checando Take Profit, Stop Loss e Timeout."""
    global in_position, automation_state, entry_price, sell_fail_count

    pair_details = automation_state.get("current_target_pair_details")

    if not in_position or not pair_details:
        return

    target_address = pair_details.get('pairAddress')
    symbol = pair_details.get('baseToken', {}).get('symbol', 'N/A')
    buy_price = entry_price # Use entry_price from global state (in USD)
    position_opened_timestamp = automation_state.get("position_opened_timestamp", 0)

    if buy_price is None or buy_price == 0.0: # Check if entry_price is set and not zero
         logger.error(f"manage_position: entry_price não definido ou é zero ({buy_price}), não é possível gerenciar a posição.")
         # Consider adding logic here to exit the position if entry_price is invalid
         return

    # Obtém os valores de Take Profit e Stop Loss da configuração
    take_profit_percentage = parameters.get("take_profit_percent")
    stop_loss_percentage = parameters.get("stop_loss_percent")

    if take_profit_percentage is None or stop_loss_percentage is None:
        logger.error("Parâmetros de Take Profit ou Stop Loss não definidos. Não é possível gerenciar a posição.")
        return

    try:
        # Puxa o preço atual da moeda
        # Use fetch_dexscreener_real_time_price que retorna priceNative e priceUsd
        price_native, current_price_usd = await fetch_dexscreener_real_time_price(target_address)

        # Decidir qual preço usar para TP/SL. Se a entrada foi em USD, usar USD. Se foi em SOL (native), usar native.
        # Assumindo que a entrada (entry_price) é em USD (baseado nos logs de compra), usamos current_price_usd
        current_price = current_price_usd
        if current_price is None or current_price == 0:
            logger.warning(f"Não foi possível obter o preço atual em USD para {symbol}.")
            # Increment sell_fail_count here as well if price fetch fails
            sell_fail_count += 1
            if sell_fail_count >= 100: # Check if price fetch failures exceed limit
                logger.error(f"ATINGIDO LIMITE DE {sell_fail_count} FALHAS AO OBTER PREÇO. RESETANDO POSIÇÃO.")
                await send_telegram_message(f"⚠️ Limite de {sell_fail_count} falhas ao obter preço para **{symbol}** atingido. Posição abandonada.")
                in_position = False
                entry_price = 0.0
                automation_state["position_opened_timestamp"] = 0
                if automation_state.get('current_target_pair_address'):
                    automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 100 # Penalize heavily on final failure
                    await send_telegram_message(f"⚠️ **{symbol}** foi penalizada por 100 ciclos após {sell_fail_count} falhas ao obter preço.")
                automation_state["current_target_pair_address"] = None
                sell_fail_count = 0 # Reset fail count after abandoning
            return

        # Calcula os preços de TP e SL com base no preço de compra (em USD)
        take_profit_price = buy_price * (1 + take_profit_percentage / 100)
        stop_loss_price = buy_price * (1 - stop_loss_percentage / 100)

        now = time.time()
        position_duration = now - position_opened_timestamp
        timeout_seconds = 300 # 5 minutes

        # Checa as condições de venda (TP, SL, Timeout)
        if current_price >= take_profit_price:
            msg = f"🟢 **TAKE PROFIT ATINGIDO!** Vendendo **{symbol}** com lucro. Valor Corrente (USD): ${current_price:.10f} Take Profit (USD): ${take_profit_price:.10f}"
            logger.info(msg.replace("**", ""))
            await send_telegram_message(msg)
            await execute_sell_order(reason="Take Profit Atingido", sell_price=current_price)
            # execute_sell_order will handle state reset and penalty if successful

        elif current_price <= stop_loss_price:
            msg = f"🔴 **STOP LOSS ATINGIDO!** Vendendo **{symbol}** para limitar o prejuízo. Valor Corrente (USD): ${current_price:.10f} Stop Loss (USD): ${stop_loss_price:.10f}"
            logger.info(msg.replace("**", ""))
            await send_telegram_message(msg)
            await execute_sell_order(reason="Stop Loss Atingido", sell_price=current_price)
            # execute_sell_order will handle state reset and penalty if successful

        elif position_duration >= timeout_seconds:
            msg = f"⏰ **TIMEOUT!** Posição em **{symbol}** aberta por mais de {timeout_seconds/60:.0f} minutos. Vendendo."
            logger.info(msg.replace("**", ""))
            await send_telegram_message(msg)
            await execute_sell_order(reason=f"Timeout de {timeout_seconds/60:.0f} minutos", sell_price=current_price)
            # execute_sell_order will handle state reset and penalty if successful

        else:
            # Continua monitorando a posição
            logger.info(f"Monitorando {symbol} | Preço atual (USD): ${current_price:,.8f} | TP: ${take_profit_price:,.8f} | SL: ${stop_loss_price:,.8f} | Tempo em posição: {position_duration:.0f}s")
            # Reset sell_fail_count if monitoring is successful (price fetched)
            sell_fail_count = 0


    except Exception as e:
        logger.error(f"Erro em manage_position: {e}", exc_info=True)
        # Increment sell_fail_count for unhandled exceptions in manage_position
        sell_fail_count += 1
        await send_telegram_message(f"⚠️ Erro em manage_position para {symbol}: {e}. Tentativa {sell_fail_count}/100. O bot permanecerá em posição.")
        if sell_fail_count >= 100: # Check limit AFTER incrementing
             logger.error(f"ATINGIDO LIMITE DE {sell_fail_count} ERROS EM manage_position. RESETANDO POSIÇÃO.")
             await send_telegram_message(f"⚠️ Limite de {sell_fail_count} erros em manage_position para **{symbol}** atingido. Posição abandonada.")
             in_position = False
             entry_price = 0.0
             automation_state["position_opened_timestamp"] = 0
             if automation_state.get('current_target_pair_address'):
                 automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 100 # Penalize heavily on final failure
                 await send_telegram_message(f"⚠️ **{symbol}** foi penalizada por 100 ciclos após {sell_fail_count} erros em manage_position.")
             automation_state["current_target_pair_address"] = None
             sell_fail_count = 0 # Reset fail count after abandoning


# ---------------- Loop autônomo completo ----------------
async def get_pair_details(pair_address):
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            res.raise_for_status()
            pair_data = res.json().get('pairs', [None])[0]
            if not pair_data:
                return None

            # Retorna o dicionário completo que a função analyze_and_score_coin espera
            # Extraímos todos os dados necessários aqui
            return pair_data

    except Exception as e:
        # Se a requisição falhar, a função retorna None, o que já é tratado no loop
        return None

async def autonomous_loop():
    """O loop principal que executa a estratégia de trade de forma autônoma, com estados de operação claros."""
    global automation_state, in_position

    logger.info("Loop autônomo iniciado.")
    automation_state["is_running"] = True # Ensure is_running is True when loop starts

    while automation_state.get("is_running", False):
        try:
            now = time.time()

            # Decrement penalty counts
            pairs_to_remove = []
            for pair_address in list(automation_state["penalty_box"].keys()):
                automation_state["penalty_box"][pair_address] -= 1
                if automation_state["penalty_box"][pair_address] <= 0:
                    pairs_to_remove.append(pair_address)

            for pair_address in pairs_to_remove:
                del automation_state["penalty_box"][pair_address]
                logger.info(f"Penalidade removida para o par: {pair_address}")

            # ------------------------------------------------------------------
            # ESTADO 1: CAÇA (Nenhum alvo selecionado)
            # ------------------------------------------------------------------
            if not automation_state.get("current_target_pair_address"):
                logger.info("Iniciando ciclo de caça...")
                # Updated function call
                gecko_approved_pairs = await discover_and_filter_pairs(pages_to_scan=10)

                if gecko_approved_pairs:
                    best_score = 0
                    best_pair_details = None

                    # Analisa e pontua todas as moedas aprovadas
                    for gecko_pair in gecko_approved_pairs:
                        pair_details = gecko_pair
                        pair_address = pair_details.get('pairAddress')
                        symbol = pair_details.get('baseToken', {}).get('symbol', 'N/A')
                        logger.info(f"Analisando par: {symbol} ({pair_address})") # Log for each analyzed pair

                        # Check if the pair is in the penalty box
                        if pair_address in automation_state["penalty_box"]:
                            logger.info(f"Pulando par penalizado: {symbol} ({pair_address}). Ciclos restantes: {automation_state['penalty_box'][pair_address]}")
                            continue

                        score = analyze_and_score_coin(pair_details)

                        if score > best_score:
                            best_score = score
                            best_pair_details = pair_details

                    # If the best coin has a score above 50 and is not in the penalty box, set it as target
                    if best_pair_details and best_score >= 50:
                        automation_state["current_target_pair_details"] = best_pair_details
                        automation_state["current_target_pair_address"] = best_pair_details["pairAddress"]
                        automation_state["target_selected_timestamp"] = now
                        analyze_and_score_coin(best_pair_details) # Explicitly call to print scoring

                        msg = f"🎯 **Novo Alvo:** {best_pair_details['baseToken']['symbol']} (Score={best_score:.2f}). Iniciando monitoramento do gatilho..."
                        logger.info(msg.replace("**", ""))
                        await send_telegram_message(msg)
                    else:
                        msg = f"⚠️ Nenhuma moeda alcançou a pontuação mínima de 50 ou as moedas elegíveis estão penalizadas. Reiniciando caça."
                        logger.info(msg)

                else:
                    logger.warning("Nenhum par em tendência passou nos filtros iniciais nesta rodada de caça.") # Updated message

                # Aguarda o intervalo de caça
                await asyncio.sleep(TRADE_INTERVAL_SECONDS)

            # ------------------------------------------------------------------
            # ESTADO 2: MONITORAMENTO (Alvo selecionado, aguardando para comprar)
            # ------------------------------------------------------------------
            elif automation_state.get("current_target_pair_address") and not in_position:
                await check_velocity_strategy()
                await asyncio.sleep(15)

            # ------------------------------------------------------------------
            # ESTADO 3: EM POSIÇÃO (Gerenciando a compra)
            # ------------------------------------------------------------------
            elif in_position:
                await manage_position()
                await asyncio.sleep(15)


        except asyncio.CancelledError:
            logger.info("Loop autônomo cancelado.")
            break
        except Exception as e:
            logger.error(f"Erro crítico no loop autônomo: {e}", exc_info=True)
            await asyncio.sleep(60)


# ---------------- Comandos Telegram ----------------
async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Bot sniper iniciado.\nUse `/set <VALOR_SOL> <STOP_LOSS_%> <TAKE_PROFIT_%> [PRIORITY_FEE]` e depois `/run`.',
        parse_mode='Markdown'
    )

async def set_params(update, context):
    if bot_running:
        await update.effective_message.reply_text("Pare o bot com /stop antes de alterar os parâmetros."); return
    try:
        args = context.args
        amount, stop_loss, take_profit = float(args[0]), float(args[1]), float(args[2])
        priority_fee = int(args[3]) if len(args) > 3 else 3000000
        parameters.update(amount=amount, stop_loss_percent=stop_loss, take_profit_percent=take_profit, priority_fee=priority_fee)
        await update.effective_message.reply_text(f"✅ Parâmetros definidos: amount={amount} SOL, stop={stop_loss}%, take_profit={take_profit}%, priority_fee={priority_fee}")
    except Exception:
        await update.effective_message.reply_text("⚠️ Formato incorreto. Uso: `/set <VALOR> <STOP_%> <TAKE_%> [PRIORITY]`", parse_mode='Markdown')

async def run_bot(update, context):
    """Inicia o loop de trade autônomo."""
    global automation_state, in_position, entry_price
    if automation_state.get("is_running", False):
        await update.effective_message.reply_text("✅ O bot já está em execução.")
        return

    # Ensure a clean state before starting
    in_position = False
    entry_price = 0.0
    automation_state.update(
        current_target_pair_address=None,
        current_target_symbol=None,
        current_target_pair_details=None,
        position_opened_timestamp=0,
        target_selected_timestamp=0,
        checking_volatility=False,
        volatility_check_start_time=0,
        penalty_box={},
        discovered_pairs={},
        took_profit_pairs=set()
    )


    automation_state["is_running"] = True
    automation_state["task"] = asyncio.create_task(autonomous_loop()) # <-- ESTA LINHA FOI RE-ADICIONADA

    logger.info("Bot de trade autônomo iniciado.")
    await update.effective_message.reply_text(
        "🚀 Bot de trade autônomo iniciado!\n"
        "O bot agora está monitorando novas pools com base nos seus critérios.\n"
        "Use /stop para parar."
    )

async def stop_bot(update, context):
    """Para o loop de trade autônomo e cancela a tarefa em execução."""
    global automation_state, in_position, entry_price

    if not automation_state.get("is_running", False):
        await update.effective_message.reply_text("O bot já está parado."); return

    # CORREÇÃO: Desliga o bot usando a variável de estado correta
    automation_state["is_running"] = False

    # Cancela a tarefa asyncio if it exists
    if "task" in automation_state and automation_state["task"]:
        automation_state["task"].cancel()

    if in_position:
        # Fetch current price to calculate P/L before selling
        pair_details = automation_state.get('current_target_pair_details', {})
        pair_address = pair_details.get('pairAddress')
        _, current_price_usd = await fetch_dexscreener_real_time_price(pair_address)
        await execute_sell_order(reason="Parada manual do bot", sell_price=current_price_usd)

    # Limpa o estado para um reinício limpo
    in_position = False
    entry_price = 0.0 # Explicitly reset entry_price
    automation_state.update(
        current_target_pair_address=None,
        current_target_symbol=None,
        current_target_pair_details=None,
        position_opened_timestamp=0,
        target_selected_timestamp=0,
        checking_volatility=False,
        volatility_check_start_time=0,
        penalty_box={},
        discovered_pairs={},
        took_profit_pairs=set()
    )


    logger.info("Bot de trade parado.")
    await update.effective_message.reply_text("🛑 Bot parado. Todas as tarefas e posições foram finalizadas.")

async def manual_buy(update, context):
    global in_position, entry_price
    if not automation_state.get("is_running", False):
        await update.effective_message.reply_text("⚠️ O bot precisa estar em execução. Use /run primeiro.")
        return
    if in_position:
        await update.effective_message.reply_text("⚠️ Já existe uma posição aberta.")
        return
    if not automation_state.get("current_target_pair_details"):
        await update.effective_message.reply_text("⚠️ O bot ainda não selecionou um alvo. Aguarde o ciclo de descoberta.")
        return
    try:
        amount = float(context.args[0])
        if amount <= 0:
            await update.effective_message.reply_text("⚠️ O valor da compra deve ser positivo.")
            return
        pair_details = automation_state["current_target_pair_details"]
        # Use priceUsd for manual buy price display
        _, price_usd = await fetch_dexscreener_real_time_price(pair_details['pairAddress'])
        if price_usd is not None:
            await update.effective_message.reply_text(f"Forçando compra manual de {amount} SOL em {pair_details['baseToken']['symbol']}...")
            # Pass the price in USD to execute_buy_order
            await execute_buy_order(amount, price_usd, pair_details, manual=True, reason="Compra Manual Forçada")
            # entry_price is set inside execute_buy_order if successful
        else:
            await update.effective_message.reply_text("⚠️ Não foi possível obter o preço atual para a compra.")
    except (IndexError, ValueError):
        await update.effective_message.reply_text("⚠️ Formato incorreto. Use: `/buy <VALOR>`", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Erro no comando /buy: {e}")
        await update.effective_message.reply_text(f"⚠️ Erro ao executar compra manual: {e}")

async def manual_sell(update, context):
    global in_position, entry_price
    if not in_position:
        await update.effective_message.reply_text("⚠️ Nenhuma posição aberta para vender.")
        return
    pair_details = automation_state.get('current_target_pair_details', {})
    pair_address = pair_details.get('pairAddress')
    _, current_price_usd = await fetch_dexscreener_real_time_price(pair_address)
    await update.effective_message.reply_text("Forçando venda manual da posição atual...")
    await execute_sell_order(reason="Venda Manual Forçada", sell_price=current_price_usd)
    # in_position and entry_price are reset inside execute_sell_order if successful


# ---------------- Main ----------------
def main():
    global application
    keep_alive()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set", set_params))
    application.add_handler(CommandHandler("run", run_bot))
    application.add_handler(CommandHandler("stop", stop_bot))
    application.add_handler(CommandHandler("buy", manual_buy))
    application.add_handler(CommandHandler("sell", manual_sell))
    logger.info("Bot do Telegram iniciado e aguardando comandos...")
    application.run_polling()

if __name__ == '__main__':
    main()
