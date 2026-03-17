# BTC/ETH 15m Telegram Alert Bot

Bot de monitoramento para **BTCUSDT** e **ETHUSDT** no **15m**, com alertas no Telegram de:

- entrada LONG/SHORT
- TP1 1:1
- TP2 2R
- TP3 3R
- stop movido para a entrada após TP1
- trailing stop no runner final
- saída final por stop ou trailing

## Estratégia implementada

### Contexto
- EMA 20 e EMA 200
- Estrutura simples
- Keltner
- POC da tendência
- VWAP ancorada na última vela de baleia

### Sentimento / fluxo
- LSR
- OI change %
- Fear & Greed
- CVD aproximado

### Regras operacionais
1. risco por trade = **0,5% da banca**
2. **TP1 = 1R**
3. realiza **40%** no TP1
4. move stop para a entrada ao bater TP1
5. realiza **25%** no TP2
6. realiza **25%** no TP3
7. deixa **10%** no trailing
8. **nunca fecha trade manualmente no código**

---

## Aviso importante
Este projeto é **paper trading / alertas**, não envia ordens para corretora.

---

## APIs usadas
- Binance Futures REST para candles
- CoinGlass API para LSR e OI (via chave em `.env`)
- Alternative.me para Fear & Greed

---

## Instalação

```bash
git clone <seu-repo>
cd btc_eth_telegram_bot
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

pip install -r requirements.txt
cp .env.example .env
```

Preencha no `.env`:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `COINGLASS_API_KEY`

Depois rode:

```bash
python main.py
```

---

## Deploy simples no GitHub + Render/Railway

1. Suba os arquivos para um repositório GitHub
2. Crie um serviço em Render ou Railway
3. Configure as variáveis do `.env` no painel
4. Comando de start:
```bash
python main.py
```

---

## Observações
- O CVD aqui é uma **aproximação por candle/volume**
- O POC é calculado com histograma de volume por faixa de preço
- A VWAP da baleia usa a última vela com volume acima da média
- Os nomes exatos de campos retornados pela CoinGlass podem variar por plano/endpoint; se necessário, ajuste as funções `fetch_lsr()` e `fetch_oi_change_pct()`
