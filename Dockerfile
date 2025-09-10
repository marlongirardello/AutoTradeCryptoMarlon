# Usa uma imagem base Python 3.12, que é a que você está usando
FROM python:3.12

# Define o diretório de trabalho dentro do contêiner
WORKDIR /app

# Instala o curl para o rustup e outras dependências
RUN apt-get update && apt-get install -y build-essential curl pkg-config libssl-dev

# Instala o Rust, necessário para algumas bibliotecas Solana
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

# Copia o arquivo de requisitos para o contêiner
COPY requirements.txt .

# Força a reinstalação das dependências e limpa o cache. Isso deve resolver o problema de conflito de versões
RUN pip cache purge
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Copia o código da aplicação
COPY . .

# Comando para rodar a aplicação
CMD ["python", "AutoCrypoMarlon.py"]
