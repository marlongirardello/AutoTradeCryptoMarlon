# Usa uma imagem base Python 3.12
FROM python:3.12

# Instala dependências do sistema
RUN apt-get update && apt-get install -y build-essential curl pkg-config libssl-dev

# Define o diretório de trabalho
WORKDIR /app

# Instala o Rust, necessário para algumas bibliotecas Solana
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

# Adiciona o diretório de binários do Rust ao PATH para que o pip possa encontrá-lo
ENV PATH="/root/.cargo/bin:${PATH}"

# Copia o arquivo de requisitos
COPY requirements.txt .

# Cria e ativa um ambiente virtual para isolar as dependências
RUN python -m venv venv
ENV PATH="/app/venv/bin:$PATH"

# Força a reinstalação de solana e solders para garantir as versões mais recentes
RUN pip install --no-cache-dir --upgrade solana solders

# Instala todas as dependências do requirements.txt
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Copia o código da aplicação
COPY . .

# Comando para rodar a aplicação no ambiente virtual
CMD ["python", "AutoCrypoMarlon.py"]
