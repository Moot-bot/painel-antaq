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

PASTA_PADRAO = os.environ.get("ANTAQ_DIR", "./final")
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


def parse_coord(s: pd.Series) -> pd.DataFrame:
    """A ANTAQ grava 'lat, lon' em graus decimais, mas o separador e a vírgula
    decimal variam entre anos — daí o parser tolerante em vez de um split."""
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
            if not (-90 <= a <= 90) or not (-180 <= b <= 180):
                a = b = np.nan
        lat.append(a)
        lon.append(b)
    return pd.DataFrame({"lat": lat, "lon": lon})


# ============================================================================
# Barra lateral
# ============================================================================

st.sidebar.title("⚓ Painel ANTAQ")
pasta = st.sidebar.text_input("Pasta dos dados", PASTA_PADRAO)
con, modo, info = conectar(pasta)

if modo == "vazio":
    st.error(
        f"Nada encontrado em `{pasta}`.\n\n"
        "Esperado `Base_Unificada_*.parquet` (modo completo) ou "
        "`agg_principal.parquet` (modo publicado). Rode o `merge_antaq.py` ou "
        "o `preparar_publicacao.py` antes.")
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
                "Origem–Destino", "Mercadorias", "Desempenho", "SQL"])


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
        st.plotly_chart(fig, use_container_width=True)
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
        st.dataframe(resumo, use_container_width=True)

    st.caption("Variação ano a ano (Mt)")
    delta = tot.diff().dropna()
    st.plotly_chart(
        px.bar(x=delta.index, y=delta.values,
               color=np.where(delta.values >= 0, "alta", "queda"),
               color_discrete_map={"alta": "#4C78A8", "queda": "#C44E52"},
               labels={"x": "", "y": "Δ Mt", "color": ""})
          .update_layout(height=250, showlegend=False),
        use_container_width=True)


# ---- 2. Sazonalidade -------------------------------------------------------
with abas[1]:
    if "Mes" not in CC:
        st.info("Coluna `Mes` ausente na base.")
    else:
        df = q(pasta, f"""SELECT AnoArquivo ano, Mes mes, SUM(peso)/1e6 mt
                          FROM carga WHERE {W} AND Mes IS NOT NULL
                          GROUP BY 1,2""")
        if df.empty:
            st.warning("Sem dados mensais no recorte.")
        else:
            mat = df.pivot_table(index="ano", columns="mes", values="mt")
            mat.columns = [MESES[int(c) - 1] for c in mat.columns]
            st.plotly_chart(
                px.imshow(mat, aspect="auto", color_continuous_scale=SEQ,
                          labels=dict(color="Mt", x="", y=""),
                          title="Peso movimentado por ano e mês")
                  .update_layout(height=460),
                use_container_width=True)
            c1, c2 = st.columns(2)
            with c1:
                perfil = df.groupby("mes").mt.mean().reindex(range(1, 13))
                perfil.index = MESES
                st.plotly_chart(
                    px.line(x=perfil.index, y=perfil.values, markers=True,
                            labels={"x": "", "y": "Mt (média dos anos)"},
                            title="Perfil sazonal médio")
                      .update_layout(height=320),
                    use_container_width=True)
            with c2:
                amp = (mat.max(axis=1) / mat.min(axis=1)).round(2)
                st.caption("Amplitude sazonal (mês mais forte ÷ mais fraco)")
                st.dataframe(amp.rename("razão"), use_container_width=True)


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
            use_container_width=True)
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
            use_container_width=True)


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
                            showcountries=True, countrycolor="#bbb",
                            showsubunits=True, subunitcolor="#ddd",
                            lataxis_range=[-35, 7], lonaxis_range=[-75, -32])
            fig.update_layout(height=620, margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig, use_container_width=True)
            st.dataframe(df[["porto", "uf", "mt", "reg"]]
                         .sort_values("mt", ascending=False).round(1),
                         use_container_width=True, hide_index=True)


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
                st.plotly_chart(fig, use_container_width=True)
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
                    use_container_width=True)
            else:
                df["par"] = df.Origem + " → " + df.Destino
                st.plotly_chart(
                    px.bar(df.sort_values("mt"), x="mt", y="par",
                           orientation="h", labels={"mt": "Mt", "par": ""},
                           color_discrete_sequence=CORES)
                      .update_layout(height=max(400, 22 * len(df))),
                    use_container_width=True)
            st.dataframe(df.round(2), use_container_width=True,
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
                    use_container_width=True)
            with c2:
                st.dataframe(df[["sh4", "natureza", "mt"]].round(1),
                             use_container_width=True, hide_index=True,
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
                use_container_width=True)


# ---- 7. Desempenho ---------------------------------------------------------
with abas[6]:
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
            use_container_width=True)

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
            st.plotly_chart(fig, use_container_width=True)
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
                    use_container_width=True)

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
                    use_container_width=True)


# ---- 8. SQL ----------------------------------------------------------------
with abas[7]:
    st.caption("SQL do DuckDB. Views: `carga`, `od`, `mercadoria`, "
               "`atracacoes`. Medidas: `peso`, `teu`, `registros`.")
    padrao = ("SELECT SGUF, SUM(peso)/1e6 AS mt\n"
              f"FROM carga\nWHERE AnoArquivo = {ano_max}\n"
              "GROUP BY 1 ORDER BY mt DESC LIMIT 20")
    sql = st.text_area("Consulta", padrao, height=170)
    if st.button("Executar", type="primary"):
        try:
            out = con.execute(sql).fetchdf()
            st.dataframe(out, use_container_width=True)
            st.download_button("Baixar CSV",
                               out.to_csv(index=False, sep=";", decimal=","),
                               "consulta_antaq.csv", "text/csv")
        except Exception as e:
            st.error(str(e))
