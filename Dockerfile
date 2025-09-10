# Usa uma imagem base Python 3.12
FROM python:3.12

# Define o diretório de trabalho
WORKDIR /app

# Instala o curl para o rustup e outras dependências
RUN apt-get update && apt-get install -y build-essential curl pkg-config libssl-dev

# Instala o Rust, necessário para algumas bibliotecas Solana
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

# Copia o arquivo de requisitos
COPY requirements.txt .

# Cria e ativa um ambiente virtual para isolar as dependências
RUN python -m venv venv
ENV PATH="/app/venv/bin:$PATH"

# Instala todas as dependências do requirements.txt dentro do ambiente virtual.
# Não há necessidade de instalar solana e solders separadamente, pois o
# requirements.txt já especifica a versão correta, e o pip resolverá a dependência.
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Copia o código da aplicação
COPY . .

# Comando para rodar a aplicação no ambiente virtual
CMD ["python", "AutoCrypoMarlon.py"]
