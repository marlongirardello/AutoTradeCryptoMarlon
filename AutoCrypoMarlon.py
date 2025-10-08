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
entry_price = 0.0

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
    
    async with httpx.AsyncClient() as client:
        try:
            quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint_str}&outputMint={output_mint_str}&amount={amount_wei}&slippageBps={slippage_bps}&maxAccounts=64"
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
            
            swap_url = "https://quote-api.jup.ag/v6/swap"
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
                return str(tx_signature)
            except Exception as confirm_e:
                logger.error(f"Transação enviada, mas falha na confirmação: {confirm_e}")
                return None

        except Exception as e:
            logger.error(f"Falha na transação: {e}")
            await send_telegram_message(f"⚠️ Falha na transação: {e}")
            return None

async def calculate_dynamic_slippage(pair_address):
    logger.info(f"Calculando slippage dinâmico para {pair_address} com base na volatilidade...")
    df = await fetch_geckoterminal_ohlcv(pair_address, "1m", limit=5)
    if df is None or df.empty or len(df) < 5:
        logger.warning("Dados insuficientes. Usando slippage padrão (5.0%).")
        return 500
    price_range = df['high'].max() - df['low'].min()
    volatility = (price_range / df['low'].min()) * 100 if df['low'].min() > 0 else 0
    if volatility > 10.0:
        slippage_bps = 1000 # 10%
    else:
        slippage_bps = 500  # 5%
    logger.info(f"Volatilidade ({volatility:.2f}%). Slippage definido para {slippage_bps/100:.2f}%.")
    return slippage_bps

# ---------------- Funções de dados reais (Dexscreener / Geckoterminal) ----------------
async def get_pair_details(pair_address):
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            res.raise_for_status()
            pair_data = res.json().get('pair')
            if not pair_data: return None
            return {
                "pair_address": pair_data['pairAddress'],
                "base_symbol": pair_data['baseToken']['symbol'],
                "quote_symbol": pair_data['quoteToken']['symbol'],
                "base_address": pair_data['baseToken']['address'],
                "quote_address": pair_data['quoteToken']['address']
            }
    except Exception:
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
    url = f"https://quote-api.jup.ag/v6/quote?inputMint={pair_details['quote_address']}&outputMint={pair_details['base_address']}&amount={test_amount_wei}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            return res.status_code == 200
    except Exception:
        return False

# ---------------- Descoberta e Filtragem (implementado conforme solicitado) ----------------
async def get_new_pools(page=1):
    """Busca uma página de novas pools da API GeckoTerminal."""
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page={page}&include=base_token,quote_token"
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

async def discover_and_filter_pairs():
    """
    Busca e filtra novos pares, analisando página por página e tratando dados ausentes 
    de forma robusta para evitar erros, de acordo com a documentação da API.
    """
    logger.info("🚀 Iniciando nova busca por pares... Analisando página por página.")
    
    for page in range(1, 11): # Analisa até 10 páginas
        logger.info(f"📄 Buscando pools da página {page}...")
        try:
            pools_page = await get_new_pools(page)
            if not pools_page:
                logger.warning(f"Nenhuma pool encontrada na página {page}.")
                await asyncio.sleep(1)
                continue

            logger.info(f"🧪 Aplicando filtros a {len(pools_page)} pools da página {page}...")
            
            for pool in pools_page:
                # --- Extração segura dos dados usando .get() ---
                attributes = pool.get('attributes', {})
                
                symbol = attributes.get('name', 'SímboloDesconhecido') 
                address = attributes.get('address')
                
                if not address:
                    continue

                liquidity = float(attributes.get('reserve_in_usd', 0))
                # O volume de 1h está dentro do objeto 'volume_usd'
                volume_h1 = float(attributes.get('volume_usd', {}).get('h1', 0))
                pool_age_str = attributes.get('pool_created_at')

                if not pool_age_str:
                    logger.warning(f"⚠️ {symbol} pulado por não ter data de criação.")
                    continue

                pool_created_at = datetime.fromisoformat(pool_age_str.replace('Z', '+00:00'))
                age_seconds = (datetime.now(timezone.utc) - pool_created_at).total_seconds()
                age_minutes = age_seconds / 60

                # --- APLICAÇÃO DOS FILTROS ---

                if liquidity < MIN_LIQUIDITY:
                    logger.info(f"❌ REJEITADO {symbol}: Liquidez ${liquidity:,.2f} < Mínimo ${MIN_LIQUIDITY:,.2f}.")
                    continue

                if volume_h1 < MIN_VOLUME_H1:
                    logger.info(f"❌ REJEITADO {symbol}: Volume 1h ${volume_h1:,.2f} < Mínimo ${MIN_VOLUME_H1:,.2f}.")
                    continue
                
                if not (900 <= age_seconds <= 3600): # Entre 15 e 60 minutos
                    logger.info(f"❌ REJEITADO {symbol}: Idade de {age_minutes:.2f} min fora do intervalo (15-60 min).")
                    continue
                
                # Extração segura dos dados de transações
                txns_h1 = attributes.get('transactions', {}).get('h1', {})
                txns_h1_buys = int(txns_h1.get('buys', 0))
                txns_h1_sells = int(txns_h1.get('sells', 0))

                if txns_h1_buys < 10 or txns_h1_sells < 3:
                    logger.info(f"❌ REJEITADO {symbol}: Transações insuficientes (Compras: {txns_h1_buys}/10, Vendas: {txns_h1_sells}/3).")
                    continue

                # ✅ APROVADO!
                logger.info(f"✅ APROVADO: {symbol} | Liquidez ${liquidity:,.2f} | Vol 1h ${volume_h1:,.2f} | Idade {age_minutes:.1f} min")
                
                # Retorna os dados necessários para a próxima etapa
                base_token_symbol = symbol.split('/')[0].strip() # Pega o símbolo do token base
                return {
                    'symbol': base_token_symbol, 
                    'address': address,
                    'liquidity': liquidity,
                    'volume_h1': volume_h1,
                    'age_minutes': age_minutes
                }
            
            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Erro inesperado ao processar a página {page}: {e}", exc_info=True)
            await asyncio.sleep(5)

    logger.info("🏁 Descoberta finalizada. Nenhuma pool passou em todos os filtros nesta rodada.")
    return None

async def analyze_and_score_coin(symbol, pair_address):
    """
    Faz análise básica de pontuação do par (momentum e volume)
    Retorna score numérico. Quanto maior, melhor.
    """
    try:
        df = await fetch_geckoterminal_ohlcv(pair_address, "1m", limit=15)
        if df is None or df.empty or len(df) < 5:
            return 0

        last_price = df['close'].iloc[-1]
        first_price = df['close'].iloc[0]
        price_change = (last_price - first_price) / first_price * 100

        avg_volume = df['volume'].mean()
        volatility = df['high'].max() - df['low'].min()

        # score simples ponderando preço, volume e estabilidade
        score = price_change * 1.5 + (avg_volume / 1_000_000) - (volatility / last_price) * 10
        logger.info(f"📊 {symbol}: Δ={price_change:.2f}% | Vol={avg_volume:,.0f} | Score={score:.2f}")
        return score
    except Exception as e:
        logger.error(f"Erro ao analisar {symbol}: {e}")
        return 0

async def find_best_coin_to_trade(pair_info):
    """
    Analisa uma única moeda aprovada, calcula sua pontuação e retorna seus detalhes.
    """
    if not pair_info or 'address' not in pair_info:
        logger.warning("Nenhuma informação de par válida recebida para análise.")
        return None, {}

    symbol = pair_info.get('symbol', 'N/A')
    address = pair_info.get('address')
    
    logger.info(f"🔎 Analisando o par aprovado: {symbol} ({address})")

    # CORREÇÃO: Chamando a função de pontuação com o nome correto ('analyze_and_score_coin')
    # e passando os argumentos que ela espera (symbol, address).
    score = await analyze_and_score_coin(symbol, address)
    
    # A função original retorna 0 em caso de erro.
    if score is None:
        logger.error(f"Não foi possível calcular a pontuação para {symbol}. Descartando.")
        return None, {}

    logger.info(f"🏆 Par único analisado: {symbol} (Score={score:.2f})")

    try:
        # A função get_pair_details já existe e busca os detalhes completos
        details = await get_pair_details(address)
        if not details:
            logger.error(f"Não foi possível obter os detalhes completos para o par {symbol} no endereço {address}")
            return None, {}
            
        details['score'] = score
        best_pair_symbol = details.get('base_symbol', symbol)
        
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
        logger.info(f"Verificação final de cotação para {pair_details['base_symbol']} antes da compra...")
        if not await is_pair_quotable_on_jupiter(pair_details):
            logger.error(f"FALHA NA COMPRA: Par {pair_details['base_symbol']} não mais negociável na Jupiter. Penalizando e buscando novo alvo.")
            await send_telegram_message(f"❌ Compra para **{pair_details['base_symbol']}** abortada. Moeda não mais negociável na Jupiter.")
            if automation_state.get("current_target_pair_address"):
                automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                automation_state["current_target_pair_address"] = None
            return

    slippage_bps = await calculate_dynamic_slippage(pair_details['pair_address'])
    logger.info(f"EXECUTANDO ORDEM DE COMPRA de {amount} SOL para {pair_details['base_symbol']} ao preço de {price}")

    tx_sig = await execute_swap(pair_details['quote_address'], pair_details['base_address'], amount, 9, slippage_bps)

    if tx_sig:
        in_position = True
        entry_price = price
        automation_state["position_opened_timestamp"] = time.time()
        sell_fail_count = 0
        buy_fail_count = 0
        log_message = (f"✅ COMPRA REALIZADA: {amount} SOL para {pair_details['base_symbol']}\n"
                       f"Motivo: {reason}\n"
                       f"Entrada: {price:.10f} | Alvo: {price * (1 + parameters['take_profit_percent']/100):.10f} | "
                       f"Stop: {price * (1 - parameters['stop_loss_percent']/100):.10f}\n"
                       f"Slippage Usado: {slippage_bps/100:.2f}%\n"
                       f"Taxa de Prioridade: {parameters.get('priority_fee')} micro-lamports\n"
                       f"https://solscan.io/tx/{tx_sig}")
        logger.info(log_message)
        await send_telegram_message(log_message)
    else:
        buy_fail_count += 1
        logger.error(f"FALHA NA EXECUÇÃO da compra para {pair_details['base_symbol']}. Tentativa {buy_fail_count}/10.")
        if buy_fail_count >= 10:
            logger.error(f"Limite de falhas de compra atingido para {pair_details['base_symbol']}. Penalizando.")
            await send_telegram_message(f"❌ FALHA NA EXECUÇÃO da compra para **{pair_details['base_symbol']}**. Limite atingido. Moeda penalizada.")
            if automation_state.get("current_target_pair_address"):
                automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                automation_state["current_target_pair_address"] = None
            buy_fail_count = 0

async def execute_sell_order(reason=""):
    global in_position, entry_price, sell_fail_count, buy_fail_count
    if not in_position: return
    
    pair_details = automation_state.get('current_target_pair_details', {})
    symbol = pair_details.get('base_symbol', 'TOKEN')
    logger.info(f"EXECUTANDO ORDEM DE VENDA de {symbol}. Motivo: {reason}")
    try:
        token_mint_pubkey = Pubkey.from_string(pair_details['base_address'])
        ata_address = get_associated_token_address(payer.pubkey(), token_mint_pubkey)
        
        balance_response = solana_client.get_token_account_balance(ata_address)

        if hasattr(balance_response, 'value'):
            token_balance_data = balance_response.value
        else:
            logger.error(f"Erro ao obter saldo do token {symbol}: Resposta RPC inválida.")
            sell_fail_count += 1
            if sell_fail_count >= 100:
                logger.error("ATINGIDO LIMITE DE FALHAS DE VENDA. RESETANDO POSIÇÃO.")
                await send_telegram_message(f"⚠️ Limite de 100 falhas de venda para **{symbol}** atingido. Posição abandonada.")
                in_position = False; entry_price = 0.0; automation_state["position_opened_timestamp"] = 0
                if automation_state.get('current_target_pair_address'):
                    automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                    automation_state["current_target_pair_address"] = None
                    await send_telegram_message(f"⚠️ **{symbol}** foi penalizada por 10 ciclos após falhas de venda.")
                sell_fail_count = 0
            await send_telegram_message(f"⚠️ Erro ao obter saldo do token {symbol}. A venda falhou. Tentativa {sell_fail_count}/100.")
            return

        amount_to_sell = token_balance_data.ui_amount
        if amount_to_sell is None or amount_to_sell == 0:
            logger.warning("Tentativa de venda com saldo zero, resetando posição.")
            in_position = False; entry_price = 0.0; automation_state["position_opened_timestamp"] = 0; automation_state["current_target_pair_address"] = None
            sell_fail_count = 0
            return

        slippage_bps = await calculate_dynamic_slippage(pair_details['pair_address'])
        tx_sig = await execute_swap(pair_details['base_address'], pair_details['quote_address'], amount_to_sell, token_balance_data.decimals, slippage_bps)
        
        if tx_sig:
            log_message = (f"🛑 VENDA REALIZADA: {symbol}\n"
                           f"Motivo: {reason}\n"
                           f"Slippage Usado: {slippage_bps/100:.2f}%\n"
                           f"Taxa de Prioridade: {parameters.get('priority_fee')} micro-lamports\n"
                           f"https://solscan.io/tx/{tx_sig}")
            logger.info(log_message)
            await send_telegram_message(log_message)
            in_position = False; entry_price = 0.0; automation_state["position_opened_timestamp"] = 0
            
            if "Stop Loss" in reason or "Timeout" in reason:
                if automation_state.get('current_target_pair_address'):
                    automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                    automation_state["current_target_pair_address"] = None
                    await send_telegram_message(f"⚠️ **{symbol}** foi penalizada por 10 ciclos após a venda por stop/timeout.")
            else:
                automation_state["current_target_pair_address"] = None
            
            sell_fail_count = 0
            buy_fail_count = 0
        else:
            logger.error(f"FALHA NA VENDA do token {symbol}. Tentativa {sell_fail_count+1}/100.")
            sell_fail_count += 1
            if sell_fail_count >= 100:
                logger.error("ATINGIDO LIMITE DE FALHAS DE VENDA. RESETANDO POSIÇÃO.")
                await send_telegram_message(f"⚠️ Limite de 100 falhas de venda para **{symbol}** atingido. Posição abandonada.")
                in_position = False; entry_price = 0.0; automation_state["position_opened_timestamp"] = 0
                if automation_state.get('current_target_pair_address'):
                    automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                    automation_state["current_target_pair_address"] = None
                    await send_telegram_message(f"⚠️ **{symbol}** foi penalizada por 10 ciclos após falhas de venda.")
                sell_fail_count = 0
            await send_telegram_message(f"❌ FALHA NA VENDA do token {symbol}. Tentativa {sell_fail_count}/100. O bot tentará novamente.")

    except Exception as e:
        logger.error(f"Erro crítico ao vender {symbol}: {e}")
        sell_fail_count += 1
        if sell_fail_count >= 100:
            logger.error("ATINGIDO LIMITE DE FALHAS DE VENDA. RESETANDO POSIÇÃO.")
            await send_telegram_message(f"⚠️ Limite de 100 falhas de venda para **{symbol}** atingido. Posição abandonada.")
            in_position = False; entry_price = 0.0; automation_state["position_opened_timestamp"] = 0
            if automation_state.get('current_target_pair_address'):
                automation_state["penalty_box"][automation_state["current_target_pair_address"]] = 10
                automation_state["current_target_pair_address"] = None
                await send_telegram_message(f"⚠️ Erro crítico ao vender {symbol}: {e}. Tentativa {sell_fail_count}/100. O bot permanecerá em posição.")

# ---------------- Estratégia velocity / momentum & adaptive timeout ----------------
async def check_velocity_strategy():
    """Analisa candles 1m do target e decide iniciar o período de observação se o sinal for forte."""
    global in_position
    target_address = automation_state.get("current_target_pair_address")
    if not target_address or in_position or automation_state.get("checking_volatility"):
        return

    pair_details = automation_state.get("current_target_pair_details")
    symbol = pair_details.get('base_symbol', 'N/A')
    data = await fetch_geckoterminal_ohlcv(target_address, parameters["timeframe"], limit=5)
    if data is None or len(data) < 2:
        return

    last = data.iloc[-1]
    price_change_pct = (last['close'] - last['open']) / last['open'] * 100 if last['open'] > 0 else 0

    # check 5m trend
    df_5m = await fetch_geckoterminal_ohlcv(target_address, "1m", limit=10)
    trend5_ok = False
    if df_5m is not None and len(df_5m) >= 5:
        try:
            df_5m['ts_dt'] = pd.to_datetime(df_5m['ts'], unit='s')
            df_5m = df_5m.set_index('ts_dt')
            close_5m = df_5m['close'].resample('5T').last().dropna()
            if len(close_5m) >= 2 and close_5m.iloc[-1] > close_5m.iloc[0]:
                trend5_ok = True
        except Exception:
            trend5_ok = False

    # --- LOGS DETALHADOS ---
    logger.info(f"🕵️ Análise de Sinal para {symbol}: Vela 1min={price_change_pct:+.2f}%, Tendência 5min={'Positiva' if trend5_ok else 'Negativa'}")

    if price_change_pct > 2.0 and trend5_ok:
        automation_state["checking_volatility"] = True
        automation_state["volatility_check_start_time"] = time.time()
        msg = f"🔔 SINAL INICIAL FORTE DETECTADO para **{symbol}**. Iniciando período de observação de 3 minutos para garantir estabilidade."
        logger.info(msg.replace("**",""))
        await send_telegram_message(msg)
    else:
        # LOG ADICIONADO: Informa por que o sinal não foi bom o suficiente
        logger.info(f"❌ Sinal para {symbol} não atendeu aos critérios (>+2% e tendência 5min positiva). Aguardando novo ciclo.")
    return

# ---------------- Loop autônomo completo ----------------
async def autonomous_loop():
    """O loop principal que executa a estratégia de trade de forma autônoma."""
    global automation_state, in_position, pair_details

    logger.info("Loop autônomo iniciado.")
    # CORREÇÃO CRÍTICA: A condição do loop agora usa a variável de estado correta.
    while automation_state.get("is_running", False):
        try:
            logger.info("Iniciando ciclo do loop autônomo...")

            # Etapa 1: Descobrir um novo par se não houver um alvo atual.
            if not automation_state.get("current_target_pair_address"):
                approved_pair = await discover_and_filter_pairs()

                if approved_pair:
                    # Etapa 2: Analisar o par encontrado.
                    best_coin_symbol, details = await find_best_coin_to_trade(approved_pair)

                    if best_coin_symbol and details:
                        pair_details = details
                        automation_state["current_target_pair_details"] = details
                        automation_state["current_target_pair_address"] = details.get('address')
                        automation_state["target_selected_timestamp"] = time.time()
                        
                        msg = f"🎯 **Novo Alvo:** {best_coin_symbol} (Score={pair_details.get('score', 0):.2f}). Iniciando monitoramento..."
                        logger.info(msg.replace("**", ""))
                        await send_telegram_message(msg)
                else:
                    logger.warning("Nenhum par novo passou nos filtros iniciais nesta rodada.")

            # Etapa 3: Se já temos um alvo, iniciar a estratégia de velocidade/compra.
            elif automation_state.get("current_target_pair_address") and not in_position:
                await check_velocity_strategy()
            
            # Etapa 4: Se já estamos em uma posição, gerenciar a posição.
            elif in_position:
                # (Seu código de gerenciamento de Take Profit / Stop Loss vai aqui)
                # Esta parte parece estar correta no seu arquivo original, pode mantê-la.
                pass

            # Aguarda o próximo ciclo
            logger.info(f"Ciclo finalizado. Aguardando {TRADE_INTERVAL_SECONDS} segundos para o próximo.")
            await asyncio.sleep(TRADE_INTERVAL_SECONDS)

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
        priority_fee = int(args[3]) if len(args) > 3 else 2000000
        parameters.update(amount=amount, stop_loss_percent=stop_loss, take_profit_percent=take_profit, priority_fee=priority_fee)
        await update.effective_message.reply_text(f"✅ Parâmetros definidos: amount={amount} SOL, stop={stop_loss}%, take_profit={take_profit}%, priority_fee={priority_fee}")
    except Exception:
        await update.effective_message.reply_text("⚠️ Formato incorreto. Uso: `/set <VALOR> <STOP_%> <TAKE_%> [PRIORITY]`", parse_mode='Markdown')

async def run_bot(update, context):
    """Inicia o loop de trade autônomo."""
    global automation_state
    if automation_state.get("is_running", False):
        await update.effective_message.reply_text("✅ O bot já está em execução.")
        return

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
    global automation_state, in_position

    if not automation_state.get("is_running", False):
        await update.effective_message.reply_text("O bot já está parado."); return

    # CORREÇÃO: Desliga o bot usando a variável de estado correta
    automation_state["is_running"] = False
    
    # Cancela a tarefa asyncio se ela existir
    if "task" in automation_state and automation_state["task"]:
        automation_state["task"].cancel()

    if in_position:
        await execute_sell_order("Parada manual do bot")

    # Limpa o estado para um reinício limpo
    automation_state.update(
        current_target_pair_address=None,
        current_target_symbol=None,
        position_opened_timestamp=0,
        target_selected_timestamp=0,
        checking_volatility=False
    )
    
    logger.info("Bot de trade parado.")
    await update.effective_message.reply_text("🛑 Bot parado. Todas as tarefas e posições foram finalizadas.")

async def manual_buy(update, context):
    if not bot_running:
        await update.effective_message.reply_text("⚠️ O bot precisa estar em execução. Use /run primeiro.")
        return
    if in_position:
        await update.effective_message.reply_text("⚠️ Já existe uma posição aberta.")
        return
    if not automation_state.get("current_target_pair_address"):
        await update.effective_message.reply_text("⚠️ O bot ainda não selecionou um alvo. Aguarde o ciclo de descoberta.")
        return
    try:
        amount = float(context.args[0])
        if amount <= 0:
            await update.effective_message.reply_text("⚠️ O valor da compra deve ser positivo.")
            return
        pair_details = automation_state["current_target_pair_details"]
        price_native, _ = await fetch_dexscreener_real_time_price(pair_details['pair_address'])
        if price_native:
            await update.effective_message.reply_text(f"Forçando compra manual de {amount} SOL em {pair_details['base_symbol']}...")
            await execute_buy_order(amount, price_native, pair_details, manual=True, reason="Compra Manual Forçada")
        else:
            await update.effective_message.reply_text("⚠️ Não foi possível obter o preço atual para a compra.")
    except (IndexError, ValueError):
        await update.effective_message.reply_text("⚠️ Formato incorreto. Use: `/buy <VALOR>`", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Erro no comando /buy: {e}")
        await update.effective_message.reply_text(f"⚠️ Erro ao executar compra manual: {e}")

async def manual_sell(update, context):
    if not in_position:
        await update.effective_message.reply_text("⚠️ Nenhuma posição aberta para vender.")
        return
    await update.effective_message.reply_text("Forçando venda manual da posição atual...")
    await execute_sell_order(reason="Venda Manual Forçada")

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

























