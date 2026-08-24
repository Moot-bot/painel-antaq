# Painel ANTAQ

Explorador do Estatístico Aquaviário da ANTAQ (2011–2025).
Streamlit + DuckDB, lendo Parquet agregado.

## Rodar localmente

```bash
pip install -r requirements.txt
streamlit run app_antaq.py
```

## Publicar de graça no Streamlit Community Cloud

1. Crie um repositório **público** vazio no GitHub (sem README, sem .gitignore).

2. Nesta pasta:

   ```bash
   git init
   git add .
   git commit -m "Painel ANTAQ"
   git branch -M main
   git remote add origin https://github.com/SEU_USUARIO/painel-antaq.git
   git push -u origin main
   ```

3. Acesse <https://share.streamlit.io> e entre com a conta do GitHub,
   autorizando o acesso aos repositórios.

4. Clique em **Create app** (canto superior direito) → **Yup, I have an app**.
   Preencha:
   - Repository: `SEU_USUARIO/painel-antaq`
   - Branch: `main`
   - Main file path: `app_antaq.py`
   - App URL: o subdomínio que quiser

5. **Deploy**. O primeiro build leva alguns minutos (instala as dependências).

Limites do plano gratuito: ~1 GB de memória, apps públicos ilimitados (apenas
1 privado), e o app hiberna após 12 h sem acesso — acorda sozinho na próxima
visita, levando alguns segundos.

O `.gitignore` desta pasta já bloqueia `final/`, `consolidado/` e
`Base_Unificada_*.parquet`, para o commit não levar a base de 220 MB por
engano.

## Dados

Os `.parquet` desta pasta são agregados (65.0 MB no total), gerados por
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
