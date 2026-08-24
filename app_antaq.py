#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Painel ANTAQ — explorador da base unificada.

Streamlit + DuckDB. O DuckDB consulta o Parquet direto do disco (pushdown de
coluna e de filtro), então nada de carregar dezenas de milhões de linhas na RAM.

Dois modos, detectados automaticamente:
  COMPLETO  — encontra `Base_Unificada_*.parquet` (grão de registro de carga).
  PUBLICADO — encontra `agg_*.parquet` (agregados leves, para deploy grátis).

O truque para não manter duas versões do app: nos dois modos são criadas as
mesmas VIEWs (`carga`, `od`, `mercadoria`, `atracacoes`) expondo as mesmas
medidas (`peso`, `teu`, `registros`). No modo completo elas são apelidos das
colunas cruas; no publicado, já vêm somadas. Todas as consultas usam SUM(),
que é correto nos dois casos.

Executar:
    pip install -r requirements.txt
    streamlit run app_antaq.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="Painel ANTAQ", page_icon="⚓", layout="wide")

# Aparece na barra lateral. Serve para confirmar, olhando o app no ar, qual
# versão do arquivo está realmente publicada — deploy que não atualizou é o
# erro mais confuso de diagnosticar.
VERSAO = "2026-08-24e"

APP_DIR = Path(__file__).resolve().parent

# O app roda em três contextos com layouts diferentes: local a partir da raiz do
# projeto (dados em ./final), local dentro da pasta publicável e no Streamlit
# Cloud (dados ao lado do .py). Em vez de fixar um caminho, procura-se nos
# candidatos prováveis, na ordem.
def _tem_dados(p: Path) -> bool:
    try:
        return (any(p.glob("Base_Unificada_*.parquet"))
                or (p / "agg_principal.parquet").exists())
    except OSError:
        return False


def descobrir_pasta() -> tuple[str, list[Path]]:
    candidatos = []
    if os.environ.get("ANTAQ_DIR"):
        candidatos.append(Path(os.environ["ANTAQ_DIR"]))
    candidatos += [APP_DIR, Path("."), Path("./final"), APP_DIR / "final",
                   APP_DIR / "dados", Path("./painel-antaq"), Path("./dados")]
    vistos, unicos = set(), []
    for c in candidatos:
        try:
            chave = c.resolve()
        except OSError:
            continue
        if chave not in vistos:
            vistos.add(chave)
            unicos.append(c)
    for c in unicos:
        if _tem_dados(c):
            return str(c), unicos
    return str(unicos[0]), unicos


PASTA_PADRAO, CANDIDATOS = descobrir_pasta()
CORES = px.colors.qualitative.Safe
SEQ = "Blues"
MESES = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
         "Jul", "Ago", "Set", "Out", "Nov", "Dez"]


# ============================================================================
# Camada de dados
# ============================================================================

@st.cache_resource
def conectar(pasta: str):
    """Abre a conexão e cria as views. Retorna (con, modo, metadados)."""
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA enable_object_cache")
    p = Path(pasta)

    completos = sorted(p.glob("Base_Unificada_*.parquet"))
    f_atr = p / "Base_Atracacoes.parquet"
    info: dict = {}

    if completos:
        modo = "completo"
        base = completos[-1].as_posix()
        info["arquivo"] = completos[-1].name
        info["mb"] = completos[-1].stat().st_size / 1e6
        con.execute(f"""
            CREATE VIEW carga AS
            SELECT *, VLPesoCargaBruta AS peso,
                   COALESCE(TEU, 0) AS teu, 1 AS registros
            FROM read_parquet('{base}')""")
        con.execute("CREATE VIEW od AS SELECT * FROM carga")
        con.execute("CREATE VIEW mercadoria AS SELECT * FROM carga")
    elif (p / "agg_principal.parquet").exists():
        modo = "publicado"
        info["arquivo"] = "agregados"
        info["mb"] = sum(f.stat().st_size for f in p.glob("agg_*.parquet")) / 1e6
        con.execute(f"CREATE VIEW carga AS SELECT * FROM "
                    f"read_parquet('{(p / 'agg_principal.parquet').as_posix()}')")
        for nome, arq in (("od", "agg_od"), ("mercadoria", "agg_mercadoria")):
            f = p / f"{arq}.parquet"
            if f.exists():
                con.execute(f"CREATE VIEW {nome} AS SELECT * FROM "
                            f"read_parquet('{f.as_posix()}')")
            else:
                con.execute(f"CREATE VIEW {nome} AS SELECT * FROM carga")
    else:
        return con, "vazio", info

    if f_atr.exists():
        con.execute(f"CREATE VIEW atracacoes AS SELECT * FROM "
                    f"read_parquet('{f_atr.as_posix()}')")
        info["tem_atracacoes"] = True
    else:
        info["tem_atracacoes"] = False

    # ---- paralisações ------------------------------------------------------
    # A Base_Atracacoes só guarda o AGREGADO por atracação (qtd, horas, 3
    # motivos concatenados). Para analisar motivo, duração e sazonalidade das
    # paradas é preciso a tabela BRUTA, uma linha por paralisação. Procura-se
    # ela em ./consolidado; no modo publicado, usa-se o agregado pré-calculado.
    info["paralisacao"] = None
    agg_par = p / "agg_paralisacao.parquet"
    brutos: list[Path] = []
    for cand in (p, p.parent / "consolidado", Path("./consolidado"),
                 Path(__file__).resolve().parent / "consolidado"):
        try:
            brutos += sorted(cand.glob("TemposAtracacaoParalisacao_*.parquet"))
        except OSError:
            continue

    if brutos and info["tem_atracacoes"]:
        con.execute(f"""
            CREATE VIEW paralisacao AS
            SELECT p.DescricaoTempoDesconto AS motivo,
                   date_diff('minute', p.DTInicio, p.DTTermino) / 60.0 AS horas,
                   1 AS paradas,
                   a."Porto Atracacao" AS porto, a.SGUF AS uf,
                   a.AnoArquivo_Atracacao AS ano, a.Mes AS mes
            FROM read_parquet('{brutos[-1].as_posix()}') p
            LEFT JOIN atracacoes a ON p.IDAtracacao = a.IDAtracacao
            WHERE p.DTInicio IS NOT NULL AND p.DTTermino IS NOT NULL
              AND date_diff('minute', p.DTInicio, p.DTTermino) >= 0""")
        info["paralisacao"] = "bruta"
    elif agg_par.exists():
        con.execute(f"CREATE VIEW paralisacao AS SELECT * FROM "
                    f"read_parquet('{agg_par.as_posix()}')")
        info["paralisacao"] = "agregada"

    return con, modo, info


def cols_de(con, view: str) -> list[str]:
    try:
        return [r[0] for r in con.execute(f"DESCRIBE {view}").fetchall()]
    except Exception:
        return []


@st.cache_data(show_spinner="Consultando…")
def q(pasta: str, sql: str) -> pd.DataFrame:
    con, _, _ = conectar(pasta)
    return con.execute(sql).fetchdf()


def lista_sql(vals) -> str:
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in vals)


MAPA_MES: dict[str, int] = {}
for _i, _nome in enumerate(
        ["janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro"], start=1):
    MAPA_MES[_nome] = _i
    MAPA_MES[_nome[:3]] = _i
    MAPA_MES[str(_i)] = _i
    MAPA_MES[f"{_i:02d}"] = _i
for _i, _nome in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"], start=1):
    MAPA_MES.setdefault(_nome, _i)
    MAPA_MES.setdefault(_nome[:3], _i)


def normalizar_mes(s: pd.Series) -> pd.Series:
    """A coluna `Mes` da ANTAQ aparece ora como número, ora como nome do mês
    por extenso, com ou sem acento. Devolve 1..12 (Int64) ou nulo."""
    t = (s.astype("string").str.strip().str.lower()
          .str.normalize("NFKD").str.encode("ascii", "ignore").str.decode("ascii"))
    num = pd.to_numeric(t, errors="coerce")
    mapeado = t.map(MAPA_MES)
    out = num.where(num.between(1, 12)).fillna(mapeado)
    return out.astype("Int64")


def parse_coord(s: pd.Series) -> pd.DataFrame:
    """Extrai lat/lon de 'Coordenadas'.

    A ordem do par não é confiável: o metadado diz "latitude e longitude", mas
    na prática aparece invertido. Em vez de assumir, o par é orientado pela
    faixa geográfica — no Brasil a latitude fica em [-34, 6] e a longitude em
    [-74, -28], que só se sobrepõem numa nesga estreita. Se uma ordem não
    couber e a outra couber, usa-se a que cabe.
    """
    LAT = (-35.0, 7.0)
    LON = (-75.0, -28.0)

    def cabe(a: float, b: float) -> bool:
        return LAT[0] <= a <= LAT[1] and LON[0] <= b <= LON[1]

    lat, lon = [], []
    for v in s.fillna(""):
        nums = re.findall(r"-?\d+[.,]?\d*", str(v).replace(";", ","))
        a = b = np.nan
        if len(nums) >= 2:
            try:
                a = float(nums[0].replace(",", "."))
                b = float(nums[1].replace(",", "."))
            except ValueError:
                a = b = np.nan
            if not np.isnan(a):
                if cabe(a, b):
                    pass                      # já está (lat, lon)
                elif cabe(b, a):
                    a, b = b, a               # veio (lon, lat)
                else:
                    a = b = np.nan            # fora do Brasil ou ilegível
        lat.append(a)
        lon.append(b)
    return pd.DataFrame({"lat": lat, "lon": lon})


# ============================================================================
# Barra lateral
# ============================================================================

st.sidebar.title("⚓ Painel ANTAQ")
st.sidebar.caption(f"versão {VERSAO}")
pasta = st.sidebar.text_input("Pasta dos dados", PASTA_PADRAO)
con, modo, info = conectar(pasta)

if modo == "vazio":
    st.error(f"Nenhum dado encontrado em `{pasta}`.")
    with st.expander("Onde eu procurei", expanded=True):
        linhas = []
        for c in CANDIDATOS:
            try:
                existe = c.is_dir()
                alvo = c.resolve()
            except OSError:
                existe, alvo = False, c
            linhas.append(f"- `{alvo}` — "
                          + ("pasta não existe" if not existe
                             else "sem os arquivos esperados"))
        st.markdown("\n".join(linhas))
        try:
            aqui = sorted(p.name for p in Path(pasta).iterdir())[:25]
            st.caption(f"Conteúdo de `{pasta}`: "
                       + (", ".join(aqui) if aqui else "(vazio)"))
        except OSError:
            pass
    st.markdown(
        "Preciso de **`Base_Unificada_*.parquet`** (modo completo) ou "
        "**`agg_principal.parquet`** (modo publicado).\n\n"
        "- Base completa: rode `merge_antaq.py -e ./consolidado -s ./final`\n"
        "- Pacote publicável: rode `preparar_publicacao.py -e ./final "
        "-s ./painel-antaq`\n\n"
        "Se os arquivos estiverem em outro lugar, informe o caminho no campo "
        "da barra lateral.")
    st.stop()

CC = cols_de(con, "carga")
CA = cols_de(con, "atracacoes")

lim = q(pasta, "SELECT MIN(AnoArquivo) a, MAX(AnoArquivo) b FROM carga")
ano_min, ano_max = int(lim.a[0]), int(lim.b[0])

anos = st.sidebar.slider("Período", ano_min, ano_max, (ano_min, ano_max))
f_nat = st.sidebar.multiselect(
    "Natureza da carga",
    q(pasta, 'SELECT DISTINCT "Natureza da Carga" v FROM carga '
             'WHERE "Natureza da Carga" IS NOT NULL ORDER BY 1').v.tolist()
    if "Natureza da Carga" in CC else [])
f_uf = st.sidebar.multiselect(
    "UF", q(pasta, "SELECT DISTINCT SGUF v FROM carga "
                   "WHERE SGUF IS NOT NULL ORDER BY 1").v.tolist()
    if "SGUF" in CC else [])
f_sent = st.sidebar.multiselect(
    "Sentido", q(pasta, "SELECT DISTINCT Sentido v FROM carga "
                        "WHERE Sentido IS NOT NULL ORDER BY 1").v.tolist()
    if "Sentido" in CC else [])
COL_NAV = "Tipo de Navegacao da Atracacao"
f_nav = st.sidebar.multiselect(
    "Tipo de navegação",
    q(pasta, f'SELECT DISTINCT "{COL_NAV}" v FROM carga '
             f'WHERE "{COL_NAV}" IS NOT NULL ORDER BY 1').v.tolist()
    if COL_NAV in CC else [])
so_rio = st.sidebar.checkbox("Somente instalações em rio (hidrovia)")
sem_dupla = st.sidebar.checkbox(
    "Só movimentação apurada",
    help="FlagMCOperacaoCarga = 1. Evita a dupla contagem da cabotagem, que "
         "gera movimentação na origem e no destino da mesma carga.")


def where(cols: list[str], col_ano: str = "AnoArquivo") -> str:
    p = [f"{col_ano} BETWEEN {anos[0]} AND {anos[1]}"]
    if f_nat and "Natureza da Carga" in cols:
        p.append(f'"Natureza da Carga" IN ({lista_sql(f_nat)})')
    if f_uf and "SGUF" in cols:
        p.append(f"SGUF IN ({lista_sql(f_uf)})")
    if f_sent and "Sentido" in cols:
        p.append(f"Sentido IN ({lista_sql(f_sent)})")
    if f_nav and COL_NAV in cols:
        p.append(f'"{COL_NAV}" IN ({lista_sql(f_nav)})')
    if so_rio and "Instalacao Portuaria em Rio" in cols:
        p.append("\"Instalacao Portuaria em Rio\" = 'Sim'")
    if sem_dupla and "FlagMCOperacaoCarga" in cols:
        p.append("\"FlagMCOperacaoCarga\" = '1'")
    return " AND ".join(p)


W = where(CC)
WA = where(CA, "AnoArquivo_Atracacao")

st.sidebar.markdown("---")
st.sidebar.caption(
    f"**Modo {modo}** · {info.get('arquivo')} · {info.get('mb', 0):,.0f} MB\n\n"
    "Peso em toneladas; contêiner cheio inclui a tara. `AnoArquivo` é o ano do "
    "arquivo de origem, não o da desatracação. Tempos vêm da base de "
    "atracações. Paralisações só existem a partir de 2015."
)


# ============================================================================
# Indicadores
# ============================================================================

kpi = q(pasta, f"""SELECT SUM(registros) reg, SUM(peso) peso, SUM(teu) teu
                   FROM carga WHERE {W}""")
katr = q(pasta, f"SELECT COUNT(*) n FROM atracacoes WHERE {WA}") \
    if info.get("tem_atracacoes") else pd.DataFrame({"n": [0]})

k1, k2, k3, k4 = st.columns(4)
k1.metric("Peso bruto", f"{(kpi.peso[0] or 0)/1e6:,.0f} Mt".replace(",", "."))
k2.metric("TEU", f"{(kpi.teu[0] or 0)/1e6:,.1f} mi".replace(",", "."))
k3.metric("Registros de carga", f"{int(kpi.reg[0] or 0):,}".replace(",", "."))
k4.metric("Atracações", f"{int(katr.n[0]):,}".replace(",", "."))

abas = st.tabs(["Visão geral", "Sazonalidade", "Portos", "Mapa",
                "Origem–Destino", "Mercadorias", "Paralisações",
                "Desempenho", "SQL"])


# ---- 1. Visão geral --------------------------------------------------------
with abas[0]:
    df = q(pasta, f'''SELECT AnoArquivo ano, "Natureza da Carga" natureza,
                             SUM(peso)/1e6 mt
                      FROM carga WHERE {W} GROUP BY 1,2 ORDER BY 1''')
    piv = df.pivot_table(index="ano", columns="natureza",
                         values="mt").fillna(0)
    c1, c2 = st.columns([3, 2])
    with c1:
        modo_g = st.radio("Visualização",
                          ["Empilhado", "Participação (%)",
                           "Índice (1º ano = 100)"],
                          horizontal=True, label_visibility="collapsed")
        if modo_g == "Empilhado":
            fig = px.area(df, x="ano", y="mt", color="natureza",
                          labels={"mt": "Mt", "ano": ""},
                          color_discrete_sequence=CORES)
        elif modo_g == "Participação (%)":
            pct = (piv.div(piv.sum(axis=1), axis=0).mul(100).reset_index()
                      .melt("ano", var_name="natureza", value_name="pct"))
            fig = px.area(pct, x="ano", y="pct", color="natureza",
                          labels={"pct": "%", "ano": ""},
                          color_discrete_sequence=CORES)
        else:
            base0 = piv.iloc[0].replace(0, np.nan)
            idx = (piv.div(base0).mul(100).reset_index()
                      .melt("ano", var_name="natureza", value_name="idx"))
            fig = px.line(idx, x="ano", y="idx", color="natureza", markers=True,
                          labels={"idx": "Índice", "ano": ""},
                          color_discrete_sequence=CORES)
            fig.add_hline(y=100, line_dash="dot", opacity=.4)
        fig.update_layout(height=420, legend_title="", hovermode="x unified")
        st.plotly_chart(fig, width="stretch")
    with c2:
        tot = piv.sum(axis=1)
        n_anos = max(len(tot) - 1, 1)
        if tot.iloc[0] > 0:
            cagr = ((tot.iloc[-1] / tot.iloc[0]) ** (1 / n_anos) - 1) * 100
            st.metric(f"CAGR {int(tot.index[0])}–{int(tot.index[-1])}",
                      f"{cagr:,.1f}% a.a.".replace(",", "."))
        base0 = piv.iloc[0].replace(0, np.nan)
        resumo = pd.DataFrame({
            "Mt (últ. ano)": piv.iloc[-1].round(1),
            "Var. total %": ((piv.iloc[-1] / base0 - 1) * 100).round(1),
            "CAGR %": (((piv.iloc[-1] / base0) ** (1 / n_anos) - 1)
                       * 100).round(1),
        }).sort_values("Mt (últ. ano)", ascending=False)
        st.dataframe(resumo, width="stretch")

    st.caption("Variação ano a ano (Mt)")
    delta = tot.diff().dropna()
    st.plotly_chart(
        px.bar(x=delta.index, y=delta.values,
               color=np.where(delta.values >= 0, "alta", "queda"),
               color_discrete_map={"alta": "#4C78A8", "queda": "#C44E52"},
               labels={"x": "", "y": "Δ Mt", "color": ""})
          .update_layout(height=250, showlegend=False),
        width="stretch")


# ---- 2. Sazonalidade -------------------------------------------------------
with abas[1]:
    if "Mes" not in CC:
        st.info("Coluna `Mes` ausente na base.")
    else:
        df = q(pasta, f"""SELECT AnoArquivo ano, Mes mes, SUM(peso)/1e6 mt
                          FROM carga WHERE {W} AND Mes IS NOT NULL
                          GROUP BY 1,2""")
        df["mes_num"] = normalizar_mes(df.mes)
        brutos = sorted(df.loc[df.mes_num.isna(), "mes"].astype(str).unique())
        df = df.dropna(subset=["mes_num"])
        df["mes_num"] = df.mes_num.astype(int)
        if brutos:
            st.caption(f"{len(brutos)} valor(es) de mês não reconhecido(s) e "
                       f"ignorado(s): {', '.join(brutos[:8])}")
        if df.empty:
            st.warning("Não consegui interpretar a coluna `Mes` desta base.")
        else:
            df = df.groupby(["ano", "mes_num"], as_index=False).mt.sum()
            mat = df.pivot_table(index="ano", columns="mes_num", values="mt")
            mat = mat.reindex(columns=range(1, 13))
            mat.columns = MESES
            st.plotly_chart(
                px.imshow(mat, aspect="auto", color_continuous_scale=SEQ,
                          labels=dict(color="Mt", x="", y=""),
                          title="Peso movimentado por ano e mês")
                  .update_layout(height=460),
                width="stretch")
            c1, c2 = st.columns(2)
            with c1:
                perfil = df.groupby("mes_num").mt.mean().reindex(range(1, 13))
                perfil.index = MESES
                st.plotly_chart(
                    px.line(x=perfil.index, y=perfil.values, markers=True,
                            labels={"x": "", "y": "Mt (média dos anos)"},
                            title="Perfil sazonal médio")
                      .update_layout(height=320),
                    width="stretch")
            with c2:
                amp = (mat.max(axis=1) / mat.min(axis=1)).round(2)
                st.caption("Amplitude sazonal (mês mais forte ÷ mais fraco)")
                st.dataframe(amp.rename("razão"), width="stretch")


# ---- 3. Portos -------------------------------------------------------------
with abas[2]:
    n = st.slider("Portos no ranking", 5, 40, 15, key="np")
    df = q(pasta, f'''SELECT "Porto Atracacao" porto, any_value(SGUF) uf,
                             SUM(peso)/1e6 mt, SUM(registros) reg
                      FROM carga WHERE {W} AND "Porto Atracacao" IS NOT NULL
                      GROUP BY 1 ORDER BY mt DESC LIMIT {n}''')
    c1, c2 = st.columns([3, 2])
    with c1:
        st.plotly_chart(
            px.bar(df.sort_values("mt"), x="mt", y="porto", orientation="h",
                   color="uf", text_auto=".0f",
                   labels={"mt": "Mt no período", "porto": ""},
                   color_discrete_sequence=CORES)
              .update_layout(height=max(400, 26 * len(df)), legend_title="UF"),
            width="stretch")
    with c2:
        tot_geral = q(pasta, f"SELECT SUM(peso)/1e6 t FROM carga "
                             f"WHERE {W}").t[0] or 0
        share = (df.mt.sum() / tot_geral * 100) if tot_geral else 0
        st.metric(f"Concentração — top {n}",
                  f"{share:,.1f}% do peso".replace(",", "."))
        todos = q(pasta, f'''SELECT "Porto Atracacao" p, SUM(peso) mt
                             FROM carga
                             WHERE {W} AND "Porto Atracacao" IS NOT NULL
                             GROUP BY 1''')
        if not todos.empty and todos.mt.sum() > 0:
            s = todos.mt / todos.mt.sum()
            st.metric("HHI (0–10.000)",
                      f"{(s.pow(2).sum() * 10_000):,.0f}".replace(",", "."),
                      help="Índice Herfindahl-Hirschman. Acima de 2.500 "
                           "indica mercado concentrado.")
            st.caption(f"{len(todos):,} portos no recorte.".replace(",", "."))

    st.markdown("**Trajetória**")
    escolha = st.multiselect("Comparar portos", df.porto.tolist(),
                             df.porto.head(5).tolist())
    if escolha:
        s = q(pasta, f'''SELECT AnoArquivo ano, "Porto Atracacao" porto,
                                SUM(peso)/1e6 mt
                         FROM carga
                         WHERE {W} AND "Porto Atracacao" IN ({lista_sql(escolha)})
                         GROUP BY 1,2 ORDER BY 1''')
        st.plotly_chart(
            px.line(s, x="ano", y="mt", color="porto", markers=True,
                    labels={"mt": "Mt", "ano": ""},
                    color_discrete_sequence=CORES).update_layout(height=380),
            width="stretch")


# ---- 4. Mapa ---------------------------------------------------------------
with abas[3]:
    if "Coordenadas" not in CC:
        st.info("Coluna `Coordenadas` ausente na base.")
    else:
        df = q(pasta, f'''SELECT "Porto Atracacao" porto, any_value(SGUF) uf,
                                 any_value(Coordenadas) coord,
                                 SUM(peso)/1e6 mt, SUM(registros) reg
                          FROM carga
                          WHERE {W} AND "Porto Atracacao" IS NOT NULL
                            AND Coordenadas IS NOT NULL
                          GROUP BY 1''')
        df = pd.concat([df.reset_index(drop=True), parse_coord(df.coord)],
                       axis=1).dropna(subset=["lat", "lon"])
        if df.empty:
            st.warning("Não consegui interpretar as coordenadas desta base.")
        else:
            st.caption(f"{len(df):,} portos localizados. Área da bolha = peso "
                       "movimentado no recorte.".replace(",", "."))
            fig = px.scatter_geo(
                df, lat="lat", lon="lon", size="mt", color="uf",
                hover_name="porto", size_max=45,
                hover_data={"mt": ":.1f", "lat": False, "lon": False},
                color_discrete_sequence=CORES)
            fig.update_geos(scope="south america", resolution=50,
                            showland=True, landcolor="#F2F0EA",
                            showocean=True, oceancolor="#EAF1F7",
                            showcountries=True, countrycolor="#C8C8C8",
                            showsubunits=True, subunitcolor="#DEDEDE",
                            coastlinecolor="#AAAAAA", fitbounds=False,
                            lataxis_range=[-34, 6], lonaxis_range=[-74, -33])
            fig.update_layout(height=620, margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig, width="stretch")
            st.dataframe(df[["porto", "uf", "mt", "reg"]]
                         .sort_values("mt", ascending=False).round(1),
                         width="stretch", hide_index=True)


# ---- 5. Origem-Destino -----------------------------------------------------
with abas[4]:
    CO = cols_de(con, "od")
    if "Origem" not in CO or "Destino" not in CO:
        st.info("Colunas Origem/Destino ausentes.")
    else:
        k = st.slider("Pares OD", 10, 60, 25, key="od")
        df = q(pasta, f'''SELECT Origem, Destino, SUM(peso)/1e6 mt
                          FROM od WHERE {where(CO)}
                            AND Origem IS NOT NULL AND Destino IS NOT NULL
                            AND Origem <> Destino
                          GROUP BY 1,2 ORDER BY mt DESC LIMIT {k}''')
        if df.empty:
            st.warning("Sem pares OD no recorte.")
        else:
            vis = st.radio("v", ["Sankey", "Matriz", "Barras"],
                           horizontal=True, label_visibility="collapsed")
            if vis == "Sankey":
                orig = [f"O: {x}" for x in df.Origem]
                dest = [f"D: {x}" for x in df.Destino]
                nos = list(dict.fromkeys(orig + dest))
                pos = {v: i for i, v in enumerate(nos)}
                fig = go.Figure(go.Sankey(
                    node=dict(label=nos, pad=12, thickness=14,
                              color="#4C78A8"),
                    link=dict(source=[pos[o] for o in orig],
                              target=[pos[d] for d in dest],
                              value=df.mt.tolist(),
                              color="rgba(76,120,168,0.35)")))
                fig.update_layout(height=max(500, 15 * len(nos)), font_size=11)
                st.plotly_chart(fig, width="stretch")
            elif vis == "Matriz":
                to = df.groupby("Origem").mt.sum().nlargest(15).index
                td = df.groupby("Destino").mt.sum().nlargest(15).index
                mat = (df[df.Origem.isin(to) & df.Destino.isin(td)]
                       .pivot_table(index="Origem", columns="Destino",
                                    values="mt"))
                st.plotly_chart(
                    px.imshow(mat, aspect="auto", color_continuous_scale=SEQ,
                              labels=dict(color="Mt"))
                      .update_layout(height=560),
                    width="stretch")
            else:
                df["par"] = df.Origem + " → " + df.Destino
                st.plotly_chart(
                    px.bar(df.sort_values("mt"), x="mt", y="par",
                           orientation="h", labels={"mt": "Mt", "par": ""},
                           color_discrete_sequence=CORES)
                      .update_layout(height=max(400, 22 * len(df))),
                    width="stretch")
            st.dataframe(df.round(2), width="stretch",
                         hide_index=True)


# ---- 6. Mercadorias --------------------------------------------------------
with abas[5]:
    CM = cols_de(con, "mercadoria")
    if "CDMercadoria" not in CM:
        st.info("Coluna CDMercadoria ausente.")
    else:
        k = st.slider("Mercadorias (SH4)", 10, 40, 20, key="sh")
        df = q(pasta, f'''SELECT CDMercadoria sh4,
                                 any_value("Natureza da Carga") natureza,
                                 SUM(peso)/1e6 mt, SUM(registros) reg
                          FROM mercadoria WHERE {where(CM)}
                            AND CDMercadoria IS NOT NULL
                          GROUP BY 1 ORDER BY mt DESC LIMIT {k}''')
        if df.empty:
            st.warning("Sem mercadorias no recorte.")
        else:
            c1, c2 = st.columns([3, 2])
            with c1:
                st.plotly_chart(
                    px.treemap(df.dropna(subset=["natureza"]),
                               path=["natureza", "sh4"], values="mt",
                               color="mt", color_continuous_scale=SEQ)
                      .update_layout(height=520,
                                     margin=dict(t=20, l=0, r=0, b=0)),
                    width="stretch")
            with c2:
                st.dataframe(df[["sh4", "natureza", "mt"]].round(1),
                             width="stretch", hide_index=True,
                             height=520)
            st.caption("Códigos NCM SH4. A descrição textual não vem nos "
                       "arquivos da ANTAQ — veja ConsultarMercadoria.aspx.")
            top = df.sh4.head(8).tolist()
            s = q(pasta, f'''SELECT AnoArquivo ano, CDMercadoria sh4,
                                    SUM(peso)/1e6 mt
                             FROM mercadoria WHERE {where(CM)}
                               AND CDMercadoria IN ({lista_sql(top)})
                             GROUP BY 1,2 ORDER BY 1''')
            st.plotly_chart(
                px.line(s, x="ano", y="mt", color="sh4", markers=True,
                        labels={"mt": "Mt", "ano": ""},
                        color_discrete_sequence=CORES)
                  .update_layout(height=380),
                width="stretch")


# ---- 7. Paralisações -------------------------------------------------------
with abas[6]:
    if info.get("paralisacao") is None:
        st.warning(
            "Não encontrei a tabela de paralisações. Ela vem de "
            "`TemposAtracacaoParalisacao_*.parquet` (pasta `consolidado`) ou de "
            "`agg_paralisacao.parquet` no modo publicado.")
    else:
        bruta = info["paralisacao"] == "bruta"
        wp = [f"ano BETWEEN {anos[0]} AND {anos[1]}"]
        if f_uf:
            wp.append(f"uf IN ({lista_sql(f_uf)})")
        WP = " AND ".join(wp)

        st.caption("A ANTAQ só registra paralisações a partir de **2015** — "
                   "anos anteriores aparecem zerados por ausência de dado, não "
                   "por ausência de parada.")

        # --- indicadores ----------------------------------------------------
        tot = q(pasta, f"""SELECT SUM(horas) horas, SUM(paradas) paradas
                           FROM paralisacao WHERE {WP}""")
        base = q(pasta, f"""SELECT COUNT(*) atr,
                        SUM(CASE WHEN QtdParalisacoes > 0 THEN 1 ELSE 0 END) com,
                        SUM(TOperacao) op
                        FROM atracacoes WHERE {WA}""") \
            if info.get("tem_atracacoes") else None

        m1, m2, m3, m4 = st.columns(4)
        horas = float(tot.horas[0] or 0)
        paradas = float(tot.paradas[0] or 0)
        m1.metric("Horas paralisadas", f"{horas/1e3:,.0f} mil".replace(",", "."))
        m2.metric("Nº de paralisações", f"{int(paradas):,}".replace(",", "."))
        if base is not None and base.atr[0]:
            m3.metric("Atracações afetadas",
                      f"{base.com[0] / base.atr[0] * 100:,.1f}%"
                      .replace(",", "."))
            if base.op[0]:
                m4.metric("Horas paradas ÷ horas de operação",
                          f"{horas / float(base.op[0]) * 100:,.1f}%"
                          .replace(",", "."),
                          help="Quanto do tempo de operação foi consumido por "
                               "paradas registradas.")

        # --- evolução -------------------------------------------------------
        ev = q(pasta, f"""SELECT ano, SUM(horas) horas, SUM(paradas) paradas
                          FROM paralisacao WHERE {WP}
                          GROUP BY 1 ORDER BY 1""")
        fig = go.Figure()
        fig.add_bar(x=ev.ano, y=ev.horas / 1e3, name="Horas paradas (mil)",
                    marker_color="#4C78A8")
        fig.add_trace(go.Scatter(x=ev.ano, y=ev.paradas / 1e3,
                                 name="Nº de paradas (mil)", yaxis="y2",
                                 mode="lines+markers",
                                 line=dict(color="#C44E52")))
        fig.update_layout(height=380, hovermode="x unified",
                          yaxis_title="Mil horas",
                          yaxis2=dict(overlaying="y", side="right",
                                      title="Mil paradas", showgrid=False),
                          legend=dict(orientation="h", y=1.12))
        st.plotly_chart(fig, width="stretch")

        # --- motivos --------------------------------------------------------
        st.markdown("**Motivos**")
        n_mot = st.slider("Quantos motivos", 5, 30, 12, key="nmot")
        mot = q(pasta, f"""SELECT motivo, SUM(horas) horas, SUM(paradas) paradas,
                                  SUM(horas)/NULLIF(SUM(paradas),0) media
                           FROM paralisacao WHERE {WP} AND motivo IS NOT NULL
                           GROUP BY 1 ORDER BY horas DESC LIMIT {n_mot}""")
        c1, c2 = st.columns([3, 2])
        with c1:
            st.plotly_chart(
                px.bar(mot.sort_values("horas"), x="horas", y="motivo",
                       orientation="h", text_auto=".2s",
                       labels={"horas": "Horas totais", "motivo": ""},
                       color_discrete_sequence=CORES)
                  .update_layout(height=max(360, 28 * len(mot))),
                width="stretch")
        with c2:
            st.caption("Frequência × duração média")
            st.plotly_chart(
                px.scatter(mot, x="paradas", y="media", size="horas",
                           hover_name="motivo", size_max=45,
                           labels={"paradas": "Nº de ocorrências",
                                   "media": "Duração média (h)"},
                           color_discrete_sequence=CORES)
                  .update_layout(height=max(360, 28 * len(mot))),
                width="stretch")
            st.caption("Canto superior esquerdo: raro mas longo. Inferior "
                       "direito: frequente mas curto — desgasta pela "
                       "repetição.")

        # --- motivo x ano ---------------------------------------------------
        top_mot = mot.motivo.head(10).tolist()
        if top_mot:
            mx = q(pasta, f"""SELECT ano, motivo, SUM(horas) horas
                              FROM paralisacao
                              WHERE {WP} AND motivo IN ({lista_sql(top_mot)})
                              GROUP BY 1,2""")
            piv = mx.pivot_table(index="motivo", columns="ano", values="horas")
            share = piv.div(piv.sum(axis=0), axis=1).mul(100)
            st.caption("Participação de cada motivo nas horas paradas do ano (%)")
            st.plotly_chart(
                px.imshow(share.round(1), aspect="auto",
                          color_continuous_scale=SEQ,
                          labels=dict(color="%", x="", y=""))
                  .update_layout(height=max(320, 30 * len(share))),
                width="stretch")

        # --- sazonalidade e portos ------------------------------------------
        c1, c2 = st.columns(2)
        with c1:
            sz = q(pasta, f"""SELECT mes, SUM(horas) horas
                              FROM paralisacao WHERE {WP} AND mes IS NOT NULL
                              GROUP BY 1""")
            sz["mes_num"] = normalizar_mes(sz.mes)
            sz = sz.dropna(subset=["mes_num"])
            if not sz.empty:
                sz = sz.groupby(sz.mes_num.astype(int)).horas.sum() \
                       .reindex(range(1, 13))
                sz.index = MESES
                st.caption("Horas paradas por mês (todos os anos)")
                st.plotly_chart(
                    px.bar(x=sz.index, y=sz.values / 1e3,
                           labels={"x": "", "y": "Mil horas"},
                           color_discrete_sequence=CORES)
                      .update_layout(height=330),
                    width="stretch")
                st.caption("Chuva costuma dominar o pico — compare com o "
                           "período úmido da região filtrada.")
        with c2:
            pr = q(pasta, f"""SELECT porto, SUM(horas) horas,
                                     SUM(paradas) paradas
                              FROM paralisacao WHERE {WP} AND porto IS NOT NULL
                              GROUP BY 1 ORDER BY horas DESC LIMIT 15""")
            st.caption("Portos com mais horas paradas")
            st.plotly_chart(
                px.bar(pr.sort_values("horas"), x="horas", y="porto",
                       orientation="h", labels={"horas": "Horas", "porto": ""},
                       color_discrete_sequence=CORES)
                  .update_layout(height=330),
                width="stretch")

        # --- distribuição (só com a tabela bruta) ---------------------------
        if bruta:
            st.markdown("**Distribuição da duração das paradas**")
            d = q(pasta, f"""SELECT
                     quantile_cont(horas, 0.50) p50,
                     quantile_cont(horas, 0.75) p75,
                     quantile_cont(horas, 0.90) p90,
                     quantile_cont(horas, 0.99) p99,
                     AVG(horas) media, MAX(horas) maximo
                     FROM paralisacao WHERE {WP}""")
            st.dataframe(d.round(2), width="stretch", hide_index=True)
            st.caption("Se a média for muito maior que a mediana, poucas "
                       "paradas longas dominam o total — e a política para "
                       "reduzi-las é diferente da que reduz paradas curtas.")
        else:
            st.caption("Modo publicado: a distribuição por parada individual "
                       "exige a tabela bruta, disponível localmente.")


# ---- 8. Desempenho ---------------------------------------------------------
with abas[7]:
    if not info.get("tem_atracacoes"):
        st.warning("`Base_Atracacoes.parquet` não encontrada.")
    else:
        st.caption("Tudo aqui é calculado no grão de **atracação**. Somar "
                   "tempos na base de carga multiplicaria pelo número de "
                   "cargas por atracação.")
        t = q(pasta, f'''SELECT AnoArquivo_Atracacao ano,
                 AVG(TEsperaAtracacao) "T1 espera fundeio",
                 AVG(TEsperaInicioOp)  "T2 atracado s/ operar",
                 AVG(TOperacao)        "T3 operação",
                 AVG(TEsperaDesatracacao) "T4 pós-operação",
                 AVG(TEstadia) estadia, COUNT(*) atracacoes
                 FROM atracacoes WHERE {WA} GROUP BY 1 ORDER BY 1''')
        etapas = ["T1 espera fundeio", "T2 atracado s/ operar",
                  "T3 operação", "T4 pós-operação"]
        longo = t.melt(id_vars="ano", value_vars=etapas,
                       var_name="etapa", value_name="horas")
        st.plotly_chart(
            px.bar(longo, x="ano", y="horas", color="etapa",
                   labels={"horas": "Horas (média)", "ano": ""},
                   color_discrete_sequence=CORES)
              .update_layout(height=420, barmode="stack", legend_title="",
                             hovermode="x unified"),
            width="stretch")

        c1, c2 = st.columns(2)
        with c1:
            st.caption("Distribuição da estadia (percentis)")
            p = q(pasta, f'''SELECT AnoArquivo_Atracacao ano,
                     quantile_cont(TEstadia, 0.25) p25,
                     quantile_cont(TEstadia, 0.50) mediana,
                     quantile_cont(TEstadia, 0.75) p75,
                     quantile_cont(TEstadia, 0.90) p90
                     FROM atracacoes WHERE {WA} GROUP BY 1 ORDER BY 1''')
            fig = go.Figure()
            for c, cor in zip(["p90", "p75", "mediana", "p25"],
                              ["#cfe0f0", "#9ec1e0", "#4C78A8", "#28496b"]):
                fig.add_trace(go.Scatter(x=p.ano, y=p[c], name=c,
                                         mode="lines+markers",
                                         line=dict(color=cor)))
            fig.update_layout(height=360, yaxis_title="Horas",
                              hovermode="x unified")
            st.plotly_chart(fig, width="stretch")
            st.caption("Fila portuária tem cauda longa: a mediana costuma "
                       "contar uma história diferente da média.")
        with c2:
            st.caption("Espera × operação por porto (bolha = nº de atracações)")
            sc = q(pasta, f'''SELECT "Porto Atracacao" porto,
                     any_value(SGUF) uf, AVG(TEsperaAtracacao) espera,
                     AVG(TOperacao) operacao, COUNT(*) n
                     FROM atracacoes WHERE {WA}
                       AND "Porto Atracacao" IS NOT NULL
                     GROUP BY 1 HAVING COUNT(*) >= 30
                     ORDER BY n DESC LIMIT 60''')
            if not sc.empty:
                st.plotly_chart(
                    px.scatter(sc, x="operacao", y="espera", size="n",
                               color="uf", hover_name="porto", size_max=40,
                               labels={"operacao": "T3 operação (h)",
                                       "espera": "T1 espera (h)"},
                               color_discrete_sequence=CORES)
                      .update_layout(height=360),
                    width="stretch")

        if "MotivosParalisacao" in CA:
            st.markdown("**Paralisações** — só há registro a partir de 2015.")
            mot = q(pasta, f'''SELECT MotivosParalisacao motivo,
                     COUNT(*) atracacoes, SUM(HorasParalisacao) horas
                     FROM atracacoes WHERE {WA}
                       AND MotivosParalisacao IS NOT NULL
                     GROUP BY 1 ORDER BY horas DESC LIMIT 15''')
            if not mot.empty:
                st.plotly_chart(
                    px.bar(mot.sort_values("horas"), x="horas", y="motivo",
                           orientation="h",
                           labels={"horas": "Horas totais", "motivo": ""},
                           color_discrete_sequence=CORES)
                      .update_layout(height=max(300, 26 * len(mot))),
                    width="stretch")


# ---- 9. SQL ----------------------------------------------------------------
with abas[8]:
    st.caption("SQL do DuckDB. Views: `carga`, `od`, `mercadoria`, "
               "`atracacoes`. Medidas: `peso`, `teu`, `registros`.")
    padrao = ("SELECT SGUF, SUM(peso)/1e6 AS mt\n"
              f"FROM carga\nWHERE AnoArquivo = {ano_max}\n"
              "GROUP BY 1 ORDER BY mt DESC LIMIT 20")
    sql = st.text_area("Consulta", padrao, height=170)
    if st.button("Executar", type="primary"):
        try:
            out = con.execute(sql).fetchdf()
            st.dataframe(out, width="stretch")
            st.download_button("Baixar CSV",
                               out.to_csv(index=False, sep=";", decimal=","),
                               "consulta_antaq.csv", "text/csv")
        except Exception as e:
            st.error(str(e))
