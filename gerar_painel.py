#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 PAINEL DE EDITORES — 30DZ7  |  gerador do dashboard "command center"
================================================================================
Lê a base "Pauta" do Notion (via API + token de integração) e gera um arquivo
HTML escuro, em tela cheia, com o desempenho do time de edição em tempo (quase)
real: quem mais entregou, quem precisou de mais versões, performance de prazos,
carga atual e a distribuição das entregas.

COMO USAR (resumo — detalhes no arquivo COMO_USAR.md):
  1) Crie uma integração em https://www.notion.so/profile/integrations
     e copie o "Internal Integration Secret" (começa com ntn_ ou secret_).
  2) Compartilhe a base "Pauta" com essa integração
     (••• no topo da base  ->  Conexões  ->  selecione sua integração).
  3) No Terminal:
        export NOTION_TOKEN="cole_seu_token_aqui"
        python3 gerar_painel.py
     -> gera o arquivo  painel_editores.html  (abra no navegador / TV).

MODO DEMONSTRAÇÃO (sem token, dados fictícios só pra ver o visual):
        python3 gerar_painel.py --demo

Requer apenas Python 3 (nenhuma instalação extra). No macOS use "python3".
================================================================================
"""

import os
import sys
import json
import random
import datetime
import urllib.request
import urllib.error

# ============================== CONFIGURAÇÃO ==================================
# Token da integração do Notion (lido da variável de ambiente NOTION_TOKEN).
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()

# ID da base "Pauta" (já preenchido com a sua base).
DATABASE_ID = "2df157e3818181798fc1db8e56c10d0e"

NOTION_VERSION = "2022-06-28"
ANO = 2026  # ano de referência do painel

# Arquivo de saída (mesma pasta deste script).
OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "painel_editores.html")

# ---- Nomes das propriedades, exatamente como aparecem no Notion ----
P_EDITOR     = "Editor"                 # multi-select
P_STATUS     = "Status Edição"          # status
P_ETAPA      = "Etapa Atual"            # select
P_TAG        = "Tag Edição"             # select  (Adiantado / Em tempo / Atrasado)
P_APROV      = "Aprovação da Entrega"   # select  (Aprovado na V1 / V2 / ...)
P_DATA_V1    = "Entrega V1 - Cliente"   # date
P_CLIENTE    = "Cliente"                # texto / relação / rollup (best-effort)
P_UNIDADE    = "Unidade"                # select / texto
P_PARENT     = "Parent item"            # relação (precisa estar preenchida)

# ---- Regras de negócio (espelham o painel nativo do Notion) ----
# "Entrega contabilizada" = tem data de Entrega V1 no ANO, é sub-item
# (Parent preenchido) e o status indica que passou da entrega.
STATUS_OPCOES_ENTREGUE = {"Aprovado", "Done", "Concluído", "Concluido"}
STATUS_GRUPOS_ENTREGUE = {"In progress", "Em andamento", "Em progresso"}
ETAPA_EDICAO = "Edição"     # usado na "Carga atual" e KPI "Em edição agora"
TAG_ATRASADO = "Atrasado"   # usado no KPI "Entregas atrasadas"

# Ordem e cores das tags de prazo no gráfico de performance.
ORDEM_TAGS = ["Adiantado", "Em tempo", "Atrasado"]

# Quantos editores mostrar nos rankings (os demais entram em "outros" na tabela).
TOP_N = 15


# ============================== API DO NOTION =================================
def _api(url, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + NOTION_TOKEN)
    req.add_header("Notion-Version", NOTION_VERSION)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        raise SystemExit(
            "\nERRO da API do Notion (%s):\n%s\n\n"
            "Cheque se: (1) o token está correto; (2) a base 'Pauta' foi "
            "compartilhada com a sua integração (••• -> Conexões)." % (e.code, detail)
        )
    except urllib.error.URLError as e:
        raise SystemExit("\nERRO de conexão com a API do Notion: %s" % e.reason)


def get_status_group_map():
    """Mapeia cada opção de status -> nome do grupo (To-do / In progress / Complete)."""
    try:
        db = _api("https://api.notion.com/v1/databases/" + DATABASE_ID)
        prop = db.get("properties", {}).get(P_STATUS, {})
        st = prop.get("status", {}) or {}
        id2name = {o["id"]: o["name"] for o in st.get("options", [])}
        opt2group = {}
        for g in st.get("groups", []):
            for oid in g.get("option_ids", []):
                opt2group[id2name.get(oid, "")] = g.get("name", "")
        return opt2group
    except SystemExit:
        raise
    except Exception as e:
        print("  (aviso: não consegui ler os grupos de status: %s)" % e)
        return {}


def fetch_rows():
    """Baixa todas as linhas da base (paginado de 100 em 100)."""
    rows, cursor, page = [], None, 0
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = _api("https://api.notion.com/v1/databases/%s/query" % DATABASE_ID,
                   "POST", body)
        rows.extend(res.get("results", []))
        page += 1
        print("  ... página %d (%d linhas acumuladas)" % (page, len(rows)))
        if res.get("has_more"):
            cursor = res.get("next_cursor")
        else:
            break
    return rows


# ===================== EXTRAÇÃO / NORMALIZAÇÃO DE LINHAS ======================
def _text_of(p):
    """Converte qualquer tipo de propriedade do Notion em texto (best-effort)."""
    if not p:
        return ""
    t = p.get("type")
    if t == "title":
        return "".join(x.get("plain_text", "") for x in p.get("title", []))
    if t == "rich_text":
        return "".join(x.get("plain_text", "") for x in p.get("rich_text", []))
    if t == "select":
        return (p.get("select") or {}).get("name", "")
    if t == "status":
        return (p.get("status") or {}).get("name", "")
    if t == "multi_select":
        return ", ".join(o.get("name", "") for o in p.get("multi_select", []))
    if t == "date":
        return (p.get("date") or {}).get("start", "") or ""
    if t == "number":
        v = p.get("number")
        return "" if v is None else str(v)
    if t == "people":
        return ", ".join(x.get("name", "") for x in p.get("people", []))
    if t == "formula":
        f = p.get("formula", {})
        return str(f.get(f.get("type"), "") or "")
    if t == "rollup":
        r = p.get("rollup", {})
        if r.get("type") == "array":
            return ", ".join(_text_of(x) for x in r.get("array", []))
        return str(r.get(r.get("type"), "") or "")
    return ""


def normalize(row):
    """Transforma uma linha da API num registro simples e uniforme."""
    props = row.get("properties", {})
    ed = props.get(P_EDITOR, {})
    editors = [o.get("name", "") for o in ed.get("multi_select", [])] if ed.get("type") == "multi_select" else []
    if not editors:
        t = _text_of(ed).strip()
        editors = [t] if t else []
    parent = props.get(P_PARENT, {})
    parent_present = bool(parent.get("relation")) if parent.get("type") == "relation" else bool(_text_of(parent))
    # título (qualquer propriedade do tipo title)
    titulo = ""
    for v in props.values():
        if v.get("type") == "title":
            titulo = _text_of(v)
            break
    return {
        "editors": [e for e in editors if e] or ["—"],
        "status": _text_of(props.get(P_STATUS, {})),
        "etapa": _text_of(props.get(P_ETAPA, {})),
        "tag": _text_of(props.get(P_TAG, {})),
        "aprov": _text_of(props.get(P_APROV, {})),
        "data_v1": _text_of(props.get(P_DATA_V1, {}))[:10],
        "cliente": _text_of(props.get(P_CLIENTE, {})),
        "unidade": _text_of(props.get(P_UNIDADE, {})),
        "parent": parent_present,
        "titulo": titulo or "(sem título)",
    }


# ============================== AGREGAÇÃO ====================================
def _date(s):
    try:
        return datetime.date.fromisoformat((s or "")[:10])
    except Exception:
        return None


def is_entregue_2026(r, opt2group):
    d = _date(r["data_v1"])
    if not d or d.year < ANO:
        return False
    if not r["parent"]:
        return False
    st = r["status"]
    grp = opt2group.get(st, "")
    return (grp in STATUS_GRUPOS_ENTREGUE) or (st in STATUS_OPCOES_ENTREGUE)


def _sort_desc(d):
    return sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))


def aggregate(records, opt2group):
    entregues = [r for r in records if is_entregue_2026(r, opt2group)]
    em_2026 = [r for r in records if (_date(r["data_v1"]) and _date(r["data_v1"]).year >= ANO and r["parent"])]
    em_edicao = [r for r in records if r["etapa"] == ETAPA_EDICAO]
    atrasadas = [r for r in records if r["tag"] == TAG_ATRASADO and _date(r["data_v1"]) and _date(r["data_v1"]).year >= ANO]

    # Ranking de entregas por editor (cada editor da linha recebe +1)
    rank = {}
    for r in entregues:
        for e in r["editors"]:
            rank[e] = rank.get(e, 0) + 1
    rank_sorted = _sort_desc(rank)
    rank_top = rank_sorted[:TOP_N]
    editor_order = [e for e, _ in rank_top]

    # Versões por editor (Aprovação da Entrega) — empilhado
    aprov_vals = sorted({r["aprov"] for r in records if r["aprov"]})
    versoes = {e: {a: 0 for a in aprov_vals} for e in editor_order}
    for r in records:
        if not r["aprov"]:
            continue
        for e in r["editors"]:
            if e in versoes:
                versoes[e][r["aprov"]] += 1

    # Performance de prazos por editor (Tag Edição) — empilhado
    tags_presentes = [t for t in ORDEM_TAGS] + sorted(
        {r["tag"] for r in em_2026 if r["tag"] and r["tag"] not in ORDEM_TAGS})
    prazos = {e: {t: 0 for t in tags_presentes} for e in editor_order}
    for r in em_2026:
        if not r["tag"]:
            continue
        for e in r["editors"]:
            if e in prazos:
                prazos[e][r["tag"]] += 1

    # Carga atual (em edição agora) por editor
    carga = {}
    for r in em_edicao:
        for e in r["editors"]:
            carga[e] = carga.get(e, 0) + 1
    carga_sorted = _sort_desc(carga)[:TOP_N]

    # Tabela detalhada (entregas de 2026, mais recentes primeiro)
    tabela = sorted(entregues, key=lambda r: r["data_v1"], reverse=True)
    tabela = [{
        "titulo": r["titulo"], "cliente": r["cliente"],
        "editor": ", ".join(r["editors"]), "etapa": r["etapa"],
        "tag": r["tag"], "aprov": r["aprov"], "data": r["data_v1"],
    } for r in tabela[:80]]

    return {
        "ano": ANO,
        "kpis": {
            "entregas": len(entregues),
            "editores": len({e for r in entregues for e in r["editors"]}),
            "em_edicao": len(em_edicao),
            "atrasadas": len(atrasadas),
        },
        "ranking": {"labels": [e for e, _ in rank_top], "values": [v for _, v in rank_top]},
        "versoes": {"editores": editor_order, "tipos": aprov_vals,
                    "series": [[versoes[e][a] for e in editor_order] for a in aprov_vals]},
        "prazos": {"editores": editor_order, "tags": tags_presentes,
                   "series": [[prazos[e][t] for e in editor_order] for t in tags_presentes]},
        "carga": {"labels": [e for e, _ in carga_sorted], "values": [v for _, v in carga_sorted]},
        "tabela": tabela,
    }


# ============================ DADOS DE EXEMPLO ================================
def demo_records():
    """Gera registros fictícios (nomes reais dos editores) só para o visual."""
    random.seed(37)
    editores = ["Amanda", "B2", "Caio", "Diego", "Eric", "Fernanda", "Jean",
                "JP", "João Vitor", "Jhordan", "Koba", "Misa", "Gustavo",
                "Mika", "Doug", "Flau"]
    clientes = ["RE/MAX", "WW Talks", "Integra", "The Whiners", "Brasil x Escócia",
                "Koba Club", "Doutor IA", "Grupo Vega", "Nova Era", "Trinta e Sete"]
    aprovs = ["Aprovado na V1", "Aprovado na V2", "Aprovado na V3+"]
    recs = []
    for e in editores:
        n = random.randint(4, 26)
        for _ in range(n):
            mes = random.randint(1, 6)
            dia = random.randint(1, 28)
            tag = random.choices(ORDEM_TAGS, weights=[2, 6, 2])[0]
            aprov = random.choices(aprovs, weights=[6, 3, 1])[0]
            recs.append({
                "editors": [e], "status": random.choice(["Aprovado", "Done"]),
                "etapa": "Concluído", "tag": tag, "aprov": aprov,
                "data_v1": "%d-%02d-%02d" % (ANO, mes, dia),
                "cliente": random.choice(clientes), "unidade": random.choice(["SP", "Curitiba"]),
                "parent": True, "titulo": "%s — Reels %02d" % (random.choice(clientes), random.randint(1, 40)),
            })
        # alguns projetos em edição agora (carga atual)
        for _ in range(random.randint(0, 4)):
            recs.append({
                "editors": [e], "status": "In progress", "etapa": ETAPA_EDICAO,
                "tag": "", "aprov": "", "data_v1": "", "cliente": random.choice(clientes),
                "unidade": "SP", "parent": True, "titulo": "Em edição — %s" % e,
            })
    return recs


# ============================== RENDER HTML ==================================
def render_html(m, demo=False):
    ts = datetime.datetime.now().strftime("%d/%m/%Y às %H:%M")
    data_json = json.dumps(m, ensure_ascii=False).replace("</", "<\\/")
    banner = ('<div class="demo">⚠️ MODO DEMONSTRAÇÃO — dados fictícios. '
              'Rode com seu token do Notion para ver os números reais.</div>') if demo else ""
    return (HTML_TEMPLATE
            .replace("__BANNER__", banner)
            .replace("__TS__", ts)
            .replace("__ANO__", str(m["ano"]))
            .replace("/*__DATA__*/", "const DATA = " + data_json + ";"))


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="600"><!-- recarrega a cada 10 min -->
<title>Painel de Editores · 30DZ7</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js"></script>
<style>
  :root{
    --bg:#070b14; --panel:rgba(20,27,45,.72); --panel-br:rgba(120,140,190,.14);
    --ink:#e8edf7; --mut:#8b97ad; --dim:#5f6a82;
    --blue:#3b82f6; --purple:#8b5cf6; --orange:#f59e0b; --red:#ef4444;
    --green:#10b981; --cyan:#22d3ee;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{
    font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
    color:var(--ink); background:var(--bg); min-height:100vh;
    background-image:
      radial-gradient(1100px 600px at 12% -8%, rgba(59,130,246,.16), transparent 60%),
      radial-gradient(1000px 620px at 100% 0%, rgba(139,92,246,.14), transparent 58%),
      radial-gradient(900px 700px at 50% 120%, rgba(34,211,238,.07), transparent 60%),
      linear-gradient(rgba(120,140,190,.045) 1px, transparent 1px),
      linear-gradient(90deg, rgba(120,140,190,.045) 1px, transparent 1px);
    background-size:auto,auto,auto,42px 42px,42px 42px;
    background-attachment:fixed;
  }
  .wrap{max-width:1640px;margin:0 auto;padding:26px 30px 56px}
  .demo{background:linear-gradient(90deg,rgba(245,158,11,.18),rgba(239,68,68,.14));
    border:1px solid rgba(245,158,11,.4);color:#ffd699;border-radius:12px;
    padding:10px 16px;margin-bottom:18px;font-size:13.5px;letter-spacing:.2px}

  /* ---------- topo ---------- */
  .top{display:flex;align-items:center;justify-content:space-between;gap:20px;
    padding-bottom:18px;margin-bottom:22px;border-bottom:1px solid var(--panel-br)}
  .brand{display:flex;align-items:center;gap:14px}
  .logo{width:42px;height:42px;border-radius:11px;display:grid;place-items:center;
    background:linear-gradient(135deg,var(--blue),var(--purple));
    box-shadow:0 6px 24px rgba(59,130,246,.45);font-weight:800;font-size:15px;color:#fff;
    letter-spacing:.5px}
  .brand h1{font-size:19px;margin:0;font-weight:700;letter-spacing:.3px}
  .brand .sub{margin:2px 0 0;font-size:12.5px;color:var(--mut);letter-spacing:.6px;
    text-transform:uppercase}
  .live{display:flex;align-items:center;gap:18px;color:var(--mut);font-size:13px}
  .pulse{display:inline-flex;align-items:center;gap:8px;color:var(--green);font-weight:600;
    letter-spacing:1.2px;font-size:12px}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--green);
    box-shadow:0 0 0 0 rgba(16,185,129,.7);animation:pulse 1.8s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(16,185,129,.6)}
    70%{box-shadow:0 0 0 10px rgba(16,185,129,0)}100%{box-shadow:0 0 0 0 rgba(16,185,129,0)}}
  .ts b{color:var(--ink);font-weight:600}

  /* ---------- KPIs ---------- */
  .kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:18px;margin-bottom:22px}
  .kpi{position:relative;overflow:hidden;background:var(--panel);border:1px solid var(--panel-br);
    border-radius:18px;padding:20px 22px;backdrop-filter:blur(8px)}
  .kpi::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--c)}
  .kpi .ic{font-size:20px;opacity:.9}
  .kpi .lab{font-size:12.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.8px;margin-top:10px}
  .kpi .val{font-size:46px;font-weight:800;line-height:1;margin-top:8px;
    font-variant-numeric:tabular-nums;letter-spacing:-1px;color:#fff;
    font-feature-settings:"tnum"}
  .kpi .cap{font-size:12px;color:var(--dim);margin-top:8px}
  .kpi .glow{position:absolute;right:-30px;top:-30px;width:120px;height:120px;border-radius:50%;
    background:var(--c);opacity:.16;filter:blur(28px)}

  /* ---------- grid de cards ---------- */
  .grid{display:grid;grid-template-columns:repeat(12,1fr);gap:18px}
  .card{background:var(--panel);border:1px solid var(--panel-br);border-radius:18px;
    padding:18px 20px;backdrop-filter:blur(8px);min-width:0}
  .card h3{margin:0;font-size:15px;font-weight:700;letter-spacing:.2px}
  .card .desc{margin:3px 0 14px;font-size:12.5px;color:var(--mut)}
  .span8{grid-column:span 8}.span6{grid-column:span 6}.span5{grid-column:span 5}
  .span7{grid-column:span 7}.span4{grid-column:span 4}.span12{grid-column:span 12}
  .chart-box{position:relative;width:100%}
  .h300{height:300px}.h340{height:340px}.h360{height:360px}

  /* ---------- tabela ---------- */
  .tbl-wrap{max-height:360px;overflow:auto;border-radius:12px;border:1px solid var(--panel-br)}
  table{width:100%;border-collapse:collapse;font-size:13px}
  thead th{position:sticky;top:0;background:#0e1424;color:var(--mut);text-align:left;
    font-weight:600;padding:11px 12px;letter-spacing:.4px;text-transform:uppercase;font-size:11px;
    border-bottom:1px solid var(--panel-br);cursor:pointer;white-space:nowrap}
  tbody td{padding:10px 12px;border-bottom:1px solid rgba(120,140,190,.08);color:#cdd6e6}
  tbody tr:hover{background:rgba(120,140,190,.06)}
  .chip{display:inline-block;padding:3px 9px;border-radius:999px;font-size:11px;font-weight:600;
    white-space:nowrap}
  .chip.t-adiantado{background:rgba(16,185,129,.16);color:#5eead4}
  .chip.t-emtempo{background:rgba(59,130,246,.16);color:#93c5fd}
  .chip.t-atrasado{background:rgba(239,68,68,.16);color:#fca5a5}
  .chip.t-na{background:rgba(120,140,190,.12);color:#9aa6bd}
  .foot{margin-top:26px;text-align:center;color:var(--dim);font-size:12px;letter-spacing:.3px}
  @media (max-width:1100px){
    .kpis{grid-template-columns:repeat(2,1fr)}
    .span8,.span6,.span5,.span7,.span4{grid-column:span 12}
  }
</style>
</head>
<body>
<div class="wrap">
  __BANNER__
  <div class="top">
    <div class="brand">
      <div class="logo">37</div>
      <div>
        <h1>Painel de Editores</h1>
        <p class="sub">30DZ7 · Pós-produção · Tempo real</p>
      </div>
    </div>
    <div class="live">
      <span class="pulse"><span class="dot"></span> AO VIVO</span>
      <span class="ts">Atualizado em <b>__TS__</b></span>
    </div>
  </div>

  <div class="kpis" id="kpis"></div>

  <div class="grid">
    <div class="card span8">
      <h3>🏆 Ranking de Entregas por Editor</h3>
      <p class="desc">Quem mais entregou em __ANO__ (entregas com V1 enviada ao cliente)</p>
      <div class="chart-box h360"><canvas id="cRank"></canvas></div>
    </div>
    <div class="card span4">
      <h3>Distribuição</h3>
      <p class="desc">Participação de cada editor nas entregas</p>
      <div class="chart-box h360"><canvas id="cDonut"></canvas></div>
    </div>

    <div class="card span6">
      <h3>Versões por Editor</h3>
      <p class="desc">Aprovado na V1 × aprovado em versões seguintes</p>
      <div class="chart-box h340"><canvas id="cVers"></canvas></div>
    </div>
    <div class="card span6">
      <h3>Performance de Prazos</h3>
      <p class="desc">Proporção Adiantado / Em tempo / Atrasado por editor</p>
      <div class="chart-box h340"><canvas id="cPraz"></canvas></div>
    </div>

    <div class="card span5">
      <h3>Carga Atual</h3>
      <p class="desc">Projetos em edição agora, por editor</p>
      <div class="chart-box h340"><canvas id="cCarga"></canvas></div>
    </div>
    <div class="card span7">
      <h3>Detalhe das Entregas</h3>
      <p class="desc">Entregas de __ANO__ — mais recentes primeiro (clique no cabeçalho para ordenar)</p>
      <div class="tbl-wrap"><table id="tbl"><thead><tr>
        <th data-k="titulo">Entrega</th><th data-k="cliente">Cliente</th>
        <th data-k="editor">Editor</th><th data-k="tag">Prazo</th>
        <th data-k="aprov">Aprovação</th><th data-k="data">Entrega V1</th>
      </tr></thead><tbody></tbody></table></div>
    </div>
  </div>

  <div class="foot">Gerado automaticamente a partir da base "Pauta" do Notion · 30DZ7</div>
</div>

<script>
/*__DATA__*/

const PAL=['#3b82f6','#8b5cf6','#f59e0b','#ef4444','#10b981','#22d3ee','#ec4899',
  '#eab308','#14b8a6','#a855f7','#f97316','#60a5fa','#34d399','#f43f5e','#84cc16'];
const TAGCOLOR={'Adiantado':'#10b981','Em tempo':'#3b82f6','Atrasado':'#ef4444'};

Chart.defaults.color='#9aa6bd';
Chart.defaults.font.family="'Inter',-apple-system,sans-serif";
Chart.defaults.font.size=12;
const GRID='rgba(120,140,190,.10)';

// ---------- KPIs ----------
const K=DATA.kpis;
const cards=[
  {ic:'📦',c:'var(--blue)',lab:'Entregas em '+DATA.ano,val:K.entregas,cap:'V1 enviada ao cliente'},
  {ic:'🎬',c:'var(--purple)',lab:'Editores ativos',val:K.editores,cap:'com entregas no ano'},
  {ic:'✂️',c:'var(--orange)',lab:'Em edição agora',val:K.em_edicao,cap:'projetos na etapa de Edição'},
  {ic:'⏰',c:'var(--red)',lab:'Entregas atrasadas',val:K.atrasadas,cap:'tag de edição = Atrasado'},
];
document.getElementById('kpis').innerHTML=cards.map(c=>
  `<div class="kpi" style="--c:${c.c}"><div class="glow"></div>
   <div class="ic">${c.ic}</div><div class="lab">${c.lab}</div>
   <div class="val" data-v="${c.val}">0</div><div class="cap">${c.cap}</div></div>`).join('');
// contagem animada
document.querySelectorAll('.kpi .val').forEach(el=>{
  const end=+el.dataset.v||0; const dur=900; const t0=performance.now();
  (function step(t){const p=Math.min(1,(t-t0)/dur);
    el.textContent=Math.round(end*(1-Math.pow(1-p,3)));
    if(p<1)requestAnimationFrame(step);})(t0);
});

// ---------- Ranking (barras horizontais) ----------
new Chart(document.getElementById('cRank'),{
  type:'bar',
  data:{labels:DATA.ranking.labels,datasets:[{data:DATA.ranking.values,
    backgroundColor:DATA.ranking.labels.map((_,i)=>PAL[i%PAL.length]),
    borderRadius:7,barThickness:'flex',maxBarThickness:26}]},
  options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,
    plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>' '+c.parsed.x+' entregas'}}},
    scales:{x:{grid:{color:GRID},ticks:{precision:0}},y:{grid:{display:false}}}}
});

// ---------- Donut ----------
new Chart(document.getElementById('cDonut'),{
  type:'doughnut',
  data:{labels:DATA.ranking.labels,datasets:[{data:DATA.ranking.values,
    backgroundColor:DATA.ranking.labels.map((_,i)=>PAL[i%PAL.length]),
    borderColor:'rgba(7,11,20,.7)',borderWidth:2}]},
  options:{responsive:true,maintainAspectRatio:false,cutout:'60%',
    plugins:{legend:{position:'right',labels:{boxWidth:11,padding:9,font:{size:11}}}}}
});

// ---------- Versões (empilhado) ----------
new Chart(document.getElementById('cVers'),{
  type:'bar',
  data:{labels:DATA.versoes.editores,datasets:DATA.versoes.tipos.map((t,i)=>({
    label:t,data:DATA.versoes.series[i],backgroundColor:PAL[i%PAL.length],
    borderRadius:5,stack:'v'}))},
  options:{responsive:true,maintainAspectRatio:false,
    plugins:{legend:{position:'top',labels:{boxWidth:11,padding:10}}},
    scales:{x:{stacked:true,grid:{display:false}},y:{stacked:true,grid:{color:GRID},ticks:{precision:0}}}}
});

// ---------- Prazos (100% empilhado) ----------
new Chart(document.getElementById('cPraz'),{
  type:'bar',
  data:{labels:DATA.prazos.editores,datasets:DATA.prazos.tags.map((t,i)=>({
    label:t,data:DATA.prazos.series[i],
    backgroundColor:TAGCOLOR[t]||PAL[(i+5)%PAL.length],borderRadius:5,stack:'p'}))},
  options:{responsive:true,maintainAspectRatio:false,
    plugins:{legend:{position:'top',labels:{boxWidth:11,padding:10}},
      tooltip:{callbacks:{label:c=>' '+c.dataset.label+': '+c.parsed.y}}},
    scales:{x:{stacked:true,grid:{display:false}},
      y:{stacked:true,grid:{color:GRID},ticks:{precision:0}}}}
});

// ---------- Carga atual ----------
new Chart(document.getElementById('cCarga'),{
  type:'bar',
  data:{labels:DATA.carga.labels,datasets:[{data:DATA.carga.values,
    backgroundColor:'#f59e0b',borderRadius:6,maxBarThickness:24}]},
  options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,
    plugins:{legend:{display:false}},
    scales:{x:{grid:{color:GRID},ticks:{precision:0}},y:{grid:{display:false}}}}
});

// ---------- Tabela ----------
const tbody=document.querySelector('#tbl tbody');
function tagClass(t){t=(t||'').toLowerCase();
  if(t.indexOf('adiant')>=0)return 't-adiantado';
  if(t.indexOf('tempo')>=0)return 't-emtempo';
  if(t.indexOf('atras')>=0)return 't-atrasado';return 't-na';}
function fmt(d){if(!d)return '—';const p=d.split('-');return p.length===3?p[2]+'/'+p[1]:d;}
let rows=DATA.tabela.slice();
function draw(){
  tbody.innerHTML=rows.map(r=>`<tr>
    <td>${r.titulo||'—'}</td><td>${r.cliente||'—'}</td><td>${r.editor||'—'}</td>
    <td><span class="chip ${tagClass(r.tag)}">${r.tag||'—'}</span></td>
    <td>${r.aprov||'—'}</td><td>${fmt(r.data)}</td></tr>`).join('');
}
draw();
let asc={};
document.querySelectorAll('#tbl thead th').forEach(th=>th.addEventListener('click',()=>{
  const k=th.dataset.k;asc[k]=!asc[k];
  rows.sort((a,b)=>((a[k]||'')<(b[k]||'')?-1:1)*(asc[k]?1:-1));draw();
}));
</script>
</body>
</html>
"""


# ================================ MAIN =======================================
def main():
    demo = "--demo" in sys.argv
    if demo:
        print("Modo demonstração — gerando com dados fictícios...")
        records = demo_records()
        opt2group = {}
    else:
        if not NOTION_TOKEN:
            raise SystemExit(
                "\nERRO: variável NOTION_TOKEN não definida.\n"
                "Defina seu token antes de rodar, por exemplo:\n"
                '    export NOTION_TOKEN="ntn_seu_token_aqui"\n'
                "Ou rode em modo visual sem token:  python3 gerar_painel.py --demo\n")
        print("Lendo grupos de status...")
        opt2group = get_status_group_map()
        print("Baixando linhas da base 'Pauta'...")
        rows = fetch_rows()
        print("Processando %d linhas..." % len(rows))
        records = [normalize(r) for r in rows]

    metrics = aggregate(records, opt2group)
    html = render_html(metrics, demo)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print("\n✅ Painel gerado: %s" % OUT_FILE)
    print("   KPIs -> entregas:%(entregas)d  editores:%(editores)d  "
          "em edição:%(em_edicao)d  atrasadas:%(atrasadas)d" % metrics["kpis"])
    print("   Abra o arquivo no navegador (ou exiba numa TV).")


if __name__ == "__main__":
    main()
