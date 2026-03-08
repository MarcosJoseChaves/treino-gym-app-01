import os
from dotenv import load_dotenv
from groq import Groq

# 1. Carrega as variáveis do .env
load_dotenv()

chave_groq = os.environ.get("GROQ_API_KEY")

if not chave_groq:
    print("Erro: Chave GROQ_API_KEY não encontrada no .env")
    exit()

# 2. Inicia o cliente da Groq
client = Groq(api_key=chave_groq)

nome_exercicio = "Agachamento Livre"
prompt = f"""
Você é um especialista em fitness. Para o exercício '{nome_exercicio}', forneça:
- 2 Dicas importantes de execução
- 2 Erros comuns a evitar

Responda de forma muito curta e direta, num formato estruturado.
"""

print("Gerando resposta da IA (Llama 3 via Groq)...\n")

# 3. Faz o pedido usando o modelo gratuito da Meta (Llama 3)
resposta = client.chat.completions.create(
    messages=[
        {
            "role": "user",
            "content": prompt,
        }
    ],
    model="llama-3.1-8b-instant", # Modelo super rápido e gratuito
)

# 4. Mostra o resultado
print("Resposta da IA:")
print("-" * 20)
print(resposta.choices[0].message.content)