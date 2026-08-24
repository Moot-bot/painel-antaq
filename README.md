# Painel ANTAQ

Explorador do Estatístico Aquaviário da ANTAQ (2011–2025).
Streamlit + DuckDB, lendo Parquet agregado.

## Rodar localmente

```bash
pip install -r requirements.txt
streamlit run app_antaq.py
```

## Publicar de graça no Streamlit Community Cloud

1. Crie um repositório no GitHub e suba esta pasta:

   ```bash
   git init
   git add .
   git commit -m "Painel ANTAQ"
   git branch -M main
   git remote add origin https://github.com/SEU_USUARIO/painel-antaq.git
   git push -u origin main
   ```

2. Entre em <https://share.streamlit.io> com a conta do GitHub.
3. **New app** → escolha o repositório, branch `main`, arquivo `app_antaq.py`.
4. **Deploy**. O primeiro build leva alguns minutos.

A URL final fica no formato `https://SEU_USUARIO-painel-antaq.streamlit.app`.

## Dados

Os `.parquet` desta pasta são agregados (64.8 MB no total), gerados por
`preparar_publicacao.py` a partir da base unificada completa. Eles alimentam
todas as visualizações do painel.

| Arquivo | Grão |
|---|---|
| `agg_principal.parquet` | ano × mês × porto × UF × natureza × sentido × navegação |
| `agg_od.parquet` | ano × origem × destino × natureza |
| `agg_mercadoria.parquet` | ano × SH4 × natureza |
| `Base_Atracacoes.parquet` | uma linha por atracação (tempos T1–T4) |

## Ressalvas de leitura

- Peso em toneladas; contêiner cheio inclui a tara do contêiner.
- `AnoArquivo` é o ano do arquivo anual de origem, não o da desatracação.
- Tempos vêm da base de atracações. Somá-los no grão de carga multiplicaria
  pelo número de cargas por atracação.
- Paralisações só têm registro a partir de 2015.
- As somas de peso incluem a dupla contagem da cabotagem; use o filtro
  "só movimentação apurada" (`FlagMCOperacaoCarga = 1`) quando for o caso.

Fonte: [ANTAQ — Estatístico Aquaviário](https://web.antaq.gov.br/estatistica/).
