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

# Cria um ambiente virtual, ativa e instala as dependências em um único comando
RUN python -m venv venv && . venv/bin/activate && pip install --no-cache-dir --upgrade -r requirements.txt

# Copia o resto do código
COPY . .

# Comando para rodar a aplicação no ambiente virtual
CMD ["/app/venv/bin/python", "AutoCrypoMarlon.py"]
