# BTC/ETH GitHub Monitor + Telegram

Projeto para rodar **no GitHub** com:

- **GitHub Actions** a cada 15 minutos
- **GitHub Pages** para o painel
- **Telegram** para alertas de entrada/saída
- estratégia 15m com:
  - EMA 20 / EMA 200
  - Keltner
  - POC
  - VWAP de vela de baleia
  - LSR
  - OI
  - Fear & Greed
  - CVD aproximado

## Estrutura

- `main.py` → coleta dados, gera sinais, simula execução, envia Telegram e grava JSON do painel
- `.github/workflows/update-monitor.yml` → roda no GitHub Actions
- `docs/index.html` → painel estático
- `docs/data/latest.json` → dados consumidos pela página
- `runtime_state.json` → estado persistido da simulação

## Como subir

1. Crie um repositório no GitHub
2. Envie estes arquivos
3. Vá em **Settings > Secrets and variables > Actions**
4. Cadastre:
   - `SYMBOLS` = `BTCUSDT,ETHUSDT`
   - `TIMEFRAME` = `15m`
   - `INITIAL_EQUITY` = `100000`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `COINGLASS_API_KEY`

## Como ativar a página

1. Vá em **Settings > Pages**
2. Em **Build and deployment**, selecione **Deploy from a branch**
3. Escolha a branch `main`
4. Escolha a pasta `/docs`

Depois a página ficará em algo como:
`https://SEU_USUARIO.github.io/SEU_REPO/`

## Como ativar o workflow

1. Vá em **Actions**
2. Habilite os workflows se necessário
3. Rode manualmente `update-monitor`
4. Depois ele seguirá no agendamento de 15 em 15 minutos

## Observações
- O painel é estático, então não é tick a tick; ele atualiza a cada execução do workflow.
- O GitHub Pages é público. Não publique segredos nem dados sensíveis.
- O CVD usado aqui é aproximado por candle/volume.
- Alguns campos da CoinGlass podem variar por plano. Se necessário, ajuste `fetch_lsr()` e `fetch_oi_change_pct()`.
