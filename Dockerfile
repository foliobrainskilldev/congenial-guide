# backend/Dockerfile

# 1. Usa a imagem oficial do Python, versão 'slim' para reduzir tamanho da imagem e tempo de build
FROM python:3.11-slim

# 2. Define o diretório de trabalho dentro do container
WORKDIR /app

# 3. Variáveis de ambiente para otimizar o Python no Docker
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 4. Instala dependências do sistema operativo necessárias (algumas essenciais para compilar o ChromaDB/C++)
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 5. Copia o ficheiro de requisitos e instala as dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 6. Copia todo o restante código-fonte para a pasta /app do container
COPY . .

# 7. Expõe a porta que o Render costuma usar por defeito (10000)
EXPOSE 10000

# 8. Comando para iniciar o servidor web assíncrono uvicorn
# Vincula ao host 0.0.0.0 para aceitar conexões externas dentro da cloud
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]