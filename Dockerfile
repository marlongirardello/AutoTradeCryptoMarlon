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

# Força a instalação das dependências, removendo o cache para evitar versões antigas
# O comando --upgrade garante que a versão mais recente e compatível será instalada
# O comando --no-cache-dir evita que o pip use versões antigas em cache
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Copia o código da aplicação
COPY . .

# Comando para rodar a aplicação
CMD ["python", "AutoCrypoMarlon.py"]
