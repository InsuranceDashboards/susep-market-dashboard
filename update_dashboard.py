#!/usr/bin/env python3
"""Atualiza o dashboard público a partir das bases de produção e balanço do SES/SUSEP.

O script usa apenas a biblioteca-padrão do Python. Ele aceita um ZIP local ou
baixa a BaseCompleta.zip oficial, agrega os dados e só substitui os arquivos de
saída depois de concluir todas as validações.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


OFFICIAL_URL = "https://www2.susep.gov.br/redarq.asp?arq=BaseCompleta.zip"
PRODUCTION_FILES = {"ses_seguros.csv", "ses_cias.csv", "ses_ramos.csv", "ses_grupos_economicos.csv"}
BALANCE_FILES = {"ses_balanco.csv"}
MEASURES = (
    "premio_direto",
    "premio_emitido2",
    "premio_ganho",
    "sinistro_ocorrido",
    "despesa_resseguros",
    "receita_resseguro",
    "desp_com",
    "rvne",
)
INDEPENDENT_GROUP_CODES = {"01225", "99999"}
FINANCIAL_ITEMS = (
    "6183",   # Prêmios emitidos, exceto planos de aposentadoria
    "4027",   # Prêmios ganhos e contribuições, exceto planos de aposentadoria
    "11232",  # Sinistros ocorridos
    "11237",  # Custos de aquisição
    "6202",   # Outras receitas e despesas operacionais
    "11238",  # Resultado com resseguro
    "4069",   # Despesas administrativas
    "4070",   # Despesas com tributos
    "518",    # Lucro líquido / prejuízo
    "3333",   # Patrimônio líquido
)
FINANCIAL_SCHEMA = (
    "premiumIssued",
    "earnedPremium",
    "claims",
    "acquisition",
    "otherOperating",
    "reinsuranceResult",
    "adminExpenses",
    "taxExpenses",
    "netIncome",
    "equity",
)


def product_taxonomy(active_branches: set[str]) -> list[dict]:
    """Retorna a taxonomia de produtos inspirada no workbook executivo.

    Os recortes usam apenas códigos públicos de ramos da SUSEP. Assim, a
    atualização mensal não depende de filtros manuais por companhia presentes
    em arquivos de referência privados.
    """

    def available(codes: list[str] | tuple[str, ...] | set[str]) -> list[str]:
        return sorted(set(codes) & active_branches)

    def sub(identifier: str, label: str, codes, breakdown: bool = True) -> dict:
        return {
            "id": identifier,
            "label": label,
            "codes": available(codes),
            "breakdown": breakdown,
        }

    aviation = available(["1597", "1528", "1535", "1537", "1574"])
    infrastructure = available(["0167", "0234", "1417", "1428", "1433", "1457", "1734"])
    liability = available(["0351"])
    property_codes = available(["0112", "0116", "0118", "0141", "0171", "0173", "0196"])
    pc = available(set(aviation) | set(infrastructure) | set(liability) | set(property_codes))
    cargo = available([
        "0621", "0622", "0623", "0627", "0628", "0632", "0638", "0644",
        "0645", "0652", "0654", "0655", "0656", "0658", "0659",
    ])
    affinity = available([
        "0114", "0195", "0520", "0524", "0743", "0929", "0969", "0977",
        "0980", "0981", "0982", "0983", "0984", "0986", "0987", "0990",
        "0991", "0993", "0996", "1061", "1065", "1068", "1329", "1369",
        "1377", "1380", "1381", "1383", "1384", "1386", "1387", "1390",
        "1391", "1396", "1601", "2293",
    ])
    surety = available([
        "0310", "0313", "0327", "0378", "0746", "0748", "0749", "0775", "0776",
    ])
    agribusiness = available([
        "1101", "1102", "1103", "1104", "1105", "1106", "1107", "1108",
        "1109", "1111", "1112", "1113", "1114", "1128", "1129", "1130",
        "1161", "1162", "1163", "1164", "1165", "1198",
    ])
    dpvat = available(["0583", "0588", "0589"])
    classified = set(pc) | set(cargo) | set(affinity) | set(surety) | set(agribusiness) | set(dpvat)
    others = available(active_branches - classified)
    all_codes = available(active_branches)

    products = [
        {
            "id": "consolidated",
            "label": "Consolidado",
            "description": "Mercado SUSEP completo, sem filtros internos por companhia",
            "codes": all_codes,
            "defaultSubproduct": "consolidated-total",
            "subproducts": [
                sub("consolidated-total", "Mercado total", all_codes, False),
                sub("consolidated-pc", "P&C", pc),
                sub("consolidated-cargo", "Cargo", cargo),
                sub("consolidated-affinity", "Affinity", affinity),
                sub("consolidated-surety", "Surety", surety),
                sub("consolidated-agri", "Agronegócio", agribusiness),
                sub("consolidated-dpvat", "DPVAT", dpvat),
                sub("consolidated-others", "Outros", others),
            ],
        },
        {
            "id": "pc",
            "label": "P&C",
            "description": "Aeronáutico, infraestrutura, responsabilidade civil e patrimonial",
            "codes": pc,
            "defaultSubproduct": "pc-total",
            "subproducts": [
                sub("pc-total", "P&C total", pc, False),
                sub("pc-aviation", "Aeronáutico", aviation),
                sub("pc-infrastructure", "Infraestrutura", infrastructure),
                sub("pc-liability", "Liability", liability),
                sub("pc-property", "Property", property_codes),
            ],
        },
        {
            "id": "aviation",
            "label": "Aeronáutico",
            "description": "RETA, cascos, responsabilidade civil, hangar e satélites",
            "codes": aviation,
            "defaultSubproduct": "aviation-total",
            "subproducts": [
                sub("aviation-total", "Aeronáutico total", aviation, False),
                sub("aviation-reta", "RETA", ["1597"]),
                sub("aviation-core", "Aeronáutico", ["1528", "1535"]),
                sub("aviation-specialty", "Hangar e satélites", ["1537", "1574"]),
            ],
        },
        {
            "id": "infrastructure",
            "label": "Infraestrutura",
            "description": "Infraestrutura, petróleo, operações portuárias e marítimo",
            "codes": infrastructure,
            "defaultSubproduct": "infrastructure-total",
            "subproducts": [
                sub("infrastructure-total", "Infraestrutura total", infrastructure, False),
                sub("infrastructure-oil", "Riscos de petróleo", ["0234", "1734"]),
                sub("infrastructure-port", "Operador portuário", ["1417"]),
                sub("infrastructure-marine", "Casco marítimo", ["1428", "1433"]),
                sub("infrastructure-engineering", "Riscos de engenharia", ["0167"]),
                sub("infrastructure-dpem", "DPEM", ["1457"]),
            ],
        },
        {
            "id": "liability",
            "label": "Liability",
            "description": "Responsabilidade civil geral",
            "codes": liability,
            "defaultSubproduct": "liability-general",
            "subproducts": [sub("liability-general", "Responsabilidade civil geral", ["0351"])],
        },
        {
            "id": "property",
            "label": "Property",
            "description": "Empresarial, riscos nomeados, operacionais e diversos",
            "codes": property_codes,
            "defaultSubproduct": "property-total",
            "subproducts": [
                sub("property-total", "Property total", property_codes, False),
                sub("property-business", "Empresarial", ["0118"]),
                sub("property-named", "Riscos nomeados e operacionais", ["0196"]),
                sub("property-diverse", "Riscos diversos", ["0171"]),
                sub("property-profit", "Lucros cessantes e bancos", ["0141", "0173"]),
                sub("property-other", "Outros patrimoniais", ["0112", "0116"]),
            ],
        },
        {
            "id": "cargo",
            "label": "Cargo",
            "description": "Transportes nacional, internacional e RC do transportador",
            "codes": cargo,
            "defaultSubproduct": "cargo-total",
            "subproducts": [
                sub("cargo-total", "Cargo total", cargo, False),
                sub("cargo-domestic", "Transporte nacional", ["0621"]),
                sub("cargo-international", "Transporte internacional", ["0622"]),
                sub("cargo-carrier", "RC transportador de carga", ["0632", "0652", "0654", "0655", "0656"]),
                sub("cargo-other", "Outras responsabilidades", ["0623", "0627", "0628", "0638", "0644", "0645", "0658", "0659"]),
            ],
        },
        {
            "id": "affinity",
            "label": "Affinity",
            "description": "Residencial, pessoas, vida, prestamista e afinidades",
            "codes": affinity,
            "defaultSubproduct": "affinity-total",
            "subproducts": [
                sub("affinity-total", "Affinity total", affinity, False),
                sub("affinity-residential", "Residencial", ["0114"]),
                sub("affinity-accident", "Acidentes pessoais", ["0520", "0981", "0982", "1381"]),
                sub("affinity-life", "Vida", ["0991", "0993", "0996", "1391", "1396", "2293"]),
                sub("affinity-credit-life", "Prestamista", ["0977", "1061", "1377"]),
                sub("affinity-stop-loss", "Stop loss", ["0743"]),
                sub("affinity-other", "Outras afinidades", ["0195", "0524", "0929", "0969", "0980", "0983", "0984", "0986", "0987", "0990", "1065", "1068", "1329", "1369", "1380", "1383", "1384", "1386", "1387", "1390", "1601"]),
            ],
        },
        {
            "id": "surety",
            "label": "Surety",
            "description": "Garantia, fiança, crédito e Financial Lines",
            "codes": surety,
            "defaultSubproduct": "surety-guarantee",
            "subproducts": [
                sub("surety-total", "Surety total", surety, False),
                sub("surety-guarantee", "Garantia total", ["0775", "0776"], False),
                sub("surety-public", "Garantia setor público", ["0775"]),
                sub("surety-private", "Garantia setor privado", ["0776"]),
                sub("surety-rent", "Fiança locatícia", ["0746"]),
                sub("surety-cyber", "Riscos cibernéticos", ["0327"]),
                sub("surety-do", "D&O", ["0310", "0313"]),
                sub("surety-eo", "E&O", ["0378"]),
                sub("surety-credit", "Crédito", ["0748", "0749"]),
            ],
        },
        {
            "id": "agribusiness",
            "label": "Agronegócio",
            "description": "Agrícola, pecuário, aquícola, florestas e rural",
            "codes": agribusiness,
            "defaultSubproduct": "agribusiness-total",
            "subproducts": [
                sub("agribusiness-total", "Agronegócio total", agribusiness, False),
                sub("agribusiness-crop", "Seguro agrícola", ["1101", "1102", "1111", "1161"]),
                sub("agribusiness-livestock", "Pecuário", ["1103", "1104", "1112", "1128"]),
                sub("agribusiness-aquaculture", "Seguro aquícola", ["1105", "1106", "1113", "1129"]),
                sub("agribusiness-forest", "Seguro florestas", ["1107", "1108", "1114", "1165"]),
                sub("agribusiness-property", "Patrimonial rural", ["1130"]),
                sub("agribusiness-pledge", "Penhor rural", ["1109", "1162", "1163"]),
                sub("agribusiness-animals", "Animais", ["1164"]),
                sub("agribusiness-life", "Vida do produtor rural", ["1198"]),
            ],
        },
        {
            "id": "dpvat",
            "label": "DPVAT",
            "description": "Ramos históricos e extintos do DPVAT",
            "codes": dpvat,
            "defaultSubproduct": "dpvat-total",
            "subproducts": [sub("dpvat-total", "DPVAT total", dpvat)],
        },
        {
            "id": "others",
            "label": "Outros",
            "description": "Ramos não classificados nas famílias acima",
            "codes": others,
            "defaultSubproduct": "others-total",
            "subproducts": [sub("others-total", "Outros ramos", others)],
        },
    ]
    return [product for product in products if product["codes"]]


def clean(value: str | None) -> str:
    return (value or "").strip()


def number(value: str | None) -> float:
    value = clean(value)
    if not value:
        return 0.0
    return float(value.replace(".", "").replace(",", "."))


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", delete=False, dir=path.parent, prefix=path.name + ".") as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def atomic_write_text(path: Path, payload: str) -> None:
    atomic_write_bytes(path, payload.encode("utf-8"))


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_zip(path: Path, required_files: set[str]) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Arquivo não encontrado: {path}")
    if path.stat().st_size < 1_000_000:
        raise ValueError(f"O arquivo parece incompleto ({path.stat().st_size:,} bytes).")
    if not zipfile.is_zipfile(path):
        raise ValueError("O arquivo recebido não é um ZIP válido.")
    with zipfile.ZipFile(path) as archive:
        found = {Path(name).name.casefold() for name in archive.namelist()}
    missing = required_files - found
    if missing:
        raise ValueError("Arquivos obrigatórios ausentes no ZIP: " + ", ".join(sorted(missing)))


def download_base(url: str, cache_dir: Path, retries: int = 3) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    destination = cache_dir / f"BaseCompleta-{stamp}.zip"
    partial = destination.with_suffix(".zip.part")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SUSEP-Market-Dashboard/1.0",
            "Accept": "application/zip,application/octet-stream,*/*",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as target:
                shutil.copyfileobj(response, target, length=1024 * 1024)
            validate_zip(partial, PRODUCTION_FILES | BALANCE_FILES)
            os.replace(partial, destination)
            return destination
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = error
            if partial.exists():
                partial.unlink()
            if attempt < retries:
                time.sleep(attempt * 2)
    raise RuntimeError(f"Falha ao baixar a Base Completa após {retries} tentativas: {last_error}")


def archive_members(archive: zipfile.ZipFile) -> dict[str, str]:
    return {Path(name).name.casefold(): name for name in archive.namelist() if not name.endswith("/")}


def text_member(archive: zipfile.ZipFile, member: str) -> io.TextIOWrapper:
    return io.TextIOWrapper(archive.open(member), encoding="cp1252", newline="")


def lookup_from_archive(
    archive: zipfile.ZipFile,
    members: dict[str, str],
    filename: str,
    code_field: str,
    name_field: str,
) -> dict[str, str]:
    result: dict[str, str] = {}
    with text_member(archive, members[filename.casefold()]) as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            code = clean(row.get(code_field))
            name = clean(row.get(name_field))
            if code:
                result[code] = name
    return result


def latest_period(archive: zipfile.ZipFile, member: str) -> tuple[str, int]:
    latest = ""
    row_count = 0
    with text_member(archive, member) as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            row_count += 1
            period = clean(row.get("damesano"))
            if len(period) == 6 and period.isdigit() and period > latest:
                latest = period
    if not latest:
        raise ValueError("Nenhum período válido encontrado em Ses_seguros.csv.")
    return latest, row_count


def simplify_company_name(name: str, code: str) -> str:
    aliases = (
        ("FAIRFAX", "Fairfax"),
        ("POTTENCIAL", "Pottencial"),
        ("PORTO SEGURO", "Porto Seguro"),
        ("TOKIO MARINE", "Tokio Marine"),
        ("JUNTO", "Junto"),
        ("AVLA", "Avla"),
        ("BERKLEY", "Berkley"),
        ("ZURICH", "Zurich"),
        ("SOMPO", "Sompo"),
        ("CHUBB", "Chubb"),
        ("SWISS RE", "Swiss Re"),
        ("AUSTRAL", "Austral"),
        ("ALLIANZ TRADE", "Allianz Trade"),
        ("EULER HERMES", "Allianz Trade"),
        ("MAPFRE", "Mapfre"),
        ("MITSUI", "Mitsui Sumitomo"),
        ("EXCELSIOR", "Excelsior"),
        ("CESCE", "Cesce"),
        ("FATOR", "Fator"),
        ("BMG", "BMG"),
        ("AIG", "AIG"),
        ("AXA", "AXA"),
        ("HDI", "HDI"),
    )
    upper = name.upper()
    for needle, alias in aliases:
        if needle in upper:
            return alias
    value = re.sub(r"\([^)]*DADOS[^)]*\)", "", upper)
    value = re.sub(r"\b(S/?A|S\.A\.|SEGUROS?|SEGURADORA|CIA|COMPANHIA)\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    if not value:
        return code
    value = value.title()
    for token in (" S.A", " Ltda", " Brasil", " Vida", " Previdência"):
        value = value.replace(token, token)
    return value[:42]


def simplify_group_name(name: str, code: str) -> str:
    aliases = (
        ("BANCO DO BRASIL", "Banco do Brasil"),
        ("BRADESCO", "Bradesco"),
        ("CAIXA", "Caixa"),
        ("ITAU", "Itaú Unibanco"),
        ("ITAÚ", "Itaú Unibanco"),
        ("PORTO", "Porto Seguro"),
        ("SUL AMERICA", "SulAmérica"),
        ("SUL AMÉRICA", "SulAmérica"),
        ("TOKIO", "Tokio Marine"),
        ("ZURICH", "Zurich"),
        ("HDI", "HDI"),
        ("MONGERAL", "Mongeral Aegon"),
        ("MITSUI", "Mitsui Sumitomo"),
    )
    upper = name.upper()
    for needle, alias in aliases:
        if needle in upper:
            return alias
    value = re.sub(r"\s+", " ", name).strip(" .-")
    return value.title()[:42] if value else code


def simplify_branch_name(name: str, code: str) -> str:
    value = re.sub(rf"^\s*{re.escape(code)}\s*-\s*", "", name).strip()
    return value or code


def build_dataset(source: Path, balance_source: Path, start_year: int) -> dict:
    validate_zip(source, PRODUCTION_FILES)
    validate_zip(balance_source, BALANCE_FILES)
    source_hash = hash_file(source)
    balance_hash = hash_file(balance_source)
    with zipfile.ZipFile(source) as archive:
        members = archive_members(archive)
        insurance_member = members["ses_seguros.csv"]
        companies_lookup = lookup_from_archive(archive, members, "ses_cias.csv", "Coenti", "Noenti")
        branches_lookup = lookup_from_archive(archive, members, "ses_ramos.csv", "coramo", "noramo")
        latest, raw_rows = latest_period(archive, insurance_member)
        first_period = f"{start_year}01"
        months = []
        cursor_year, cursor_month = start_year, 1
        while f"{cursor_year}{cursor_month:02d}" <= latest:
            months.append(f"{cursor_year}{cursor_month:02d}")
            cursor_month += 1
            if cursor_month == 13:
                cursor_year += 1
                cursor_month = 1
        month_set = set(months)
        group_membership: dict[tuple[str, str], tuple[str, str]] = {}
        group_labels: dict[str, str] = {}
        with text_member(archive, members["ses_grupos_economicos.csv"]) as handle:
            reader = csv.DictReader(handle, delimiter=";")
            for row in reader:
                period = clean(row.get("damesano"))
                company = clean(row.get("coenti"))
                if period not in month_set or not company:
                    continue
                group_code = clean(row.get("cogrupo"))
                group_name = clean(row.get("nogrupo"))
                if not group_code or group_code in INDEPENDENT_GROUP_CODES:
                    group_key = "c:" + company
                    label = simplify_company_name(clean(row.get("noenti")) or companies_lookup.get(company, company), company)
                else:
                    group_key = "g:" + group_code
                    label = simplify_group_name(group_name, group_code)
                group_membership[(period, company)] = (group_key, label)
                group_labels[group_key] = label
        aggregates: dict[tuple[str, str, str], list[float]] = defaultdict(lambda: [0.0] * len(MEASURES))
        with text_member(archive, insurance_member) as handle:
            reader = csv.DictReader(handle, delimiter=";")
            missing = [measure for measure in ("damesano", "coenti", "coramo", *MEASURES) if measure not in (reader.fieldnames or [])]
            if missing:
                raise ValueError("Campos obrigatórios ausentes em Ses_seguros.csv: " + ", ".join(missing))
            for row in reader:
                period = clean(row.get("damesano"))
                if period < first_period or period > latest or period not in month_set:
                    continue
                company = clean(row.get("coenti"))
                branch = clean(row.get("coramo"))
                if not company or not branch:
                    continue
                bucket = aggregates[(period, company, branch)]
                for index, measure in enumerate(MEASURES):
                    bucket[index] += number(row.get(measure))

    financial_aggregates: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0] * len(FINANCIAL_ITEMS))
    balance_rows = 0
    with zipfile.ZipFile(balance_source) as balance_archive:
        balance_members = archive_members(balance_archive)
        with text_member(balance_archive, balance_members["ses_balanco.csv"]) as handle:
            reader = csv.DictReader(handle, delimiter=";")
            missing = [field for field in ("coenti", "damesano", "cmpid", "valor") if field not in (reader.fieldnames or [])]
            if missing:
                raise ValueError("Campos obrigatórios ausentes em Ses_Balanco.csv: " + ", ".join(missing))
            item_index = {item: index for index, item in enumerate(FINANCIAL_ITEMS)}
            for row in reader:
                balance_rows += 1
                period = clean(row.get("damesano"))
                item = clean(row.get("cmpid"))
                if period not in month_set or item not in item_index:
                    continue
                company = clean(row.get("coenti"))
                if not company:
                    continue
                membership = group_membership.get((period, company))
                if membership:
                    group_key, label = membership
                else:
                    group_key = "c:" + company
                    label = simplify_company_name(companies_lookup.get(company, company), company)
                    group_labels[group_key] = label
                financial_aggregates[(period, group_key)][item_index[item]] += number(row.get("valor"))

    compact_rows: list[list[int]] = []
    active_companies: set[str] = set()
    active_branches: set[str] = set()
    prepared: list[tuple[str, str, str, list[int]]] = []
    for (period, company, branch), values in aggregates.items():
        rounded = [round(value) for value in values]
        if not any(rounded):
            continue
        active_companies.add(company)
        active_branches.add(branch)
        prepared.append((period, company, branch, rounded))

    companies = sorted(active_companies, key=lambda code: (simplify_company_name(companies_lookup.get(code, code), code), code))
    branches = sorted(active_branches, key=lambda code: (simplify_branch_name(branches_lookup.get(code, code), code), code))
    month_index = {period: index for index, period in enumerate(months)}
    company_index = {code: index for index, code in enumerate(companies)}
    branch_index = {code: index for index, code in enumerate(branches)}
    for period, company, branch, values in prepared:
        compact_rows.append([month_index[period], company_index[company], branch_index[branch], *values])
    compact_rows.sort(key=lambda item: (item[0], item[1], item[2]))

    # A companhia em foco inicial e neutra é a líder em prêmio emitido no
    # Seguro Garantia (0775 + 0776) no YTD mais recente. O usuário pode trocar
    # livremente a seguradora no painel.
    latest_year = latest[:4]
    guarantee_totals: dict[str, int] = defaultdict(int)
    overall_totals: dict[str, int] = defaultdict(int)
    for period, company, branch, values in prepared:
        if not period.startswith(latest_year):
            continue
        overall_totals[company] += values[1]
        if branch in {"0775", "0776"}:
            guarantee_totals[company] += values[1]
    default_focus_pool = guarantee_totals or overall_totals
    if not default_focus_pool:
        raise ValueError("Não foi possível identificar uma seguradora foco para o período mais recente.")
    default_focus_code = max(default_focus_pool, key=default_focus_pool.get)

    company_names = {code: simplify_company_name(companies_lookup.get(code, code), code) for code in companies}
    name_counts: dict[str, int] = defaultdict(int)
    for name in company_names.values():
        name_counts[name] += 1
    for code, name in list(company_names.items()):
        if name_counts[name] > 1:
            company_names[code] = f"{name} · {code}"

    active_financial_groups = sorted(
        {group for _, group in financial_aggregates},
        key=lambda key: (group_labels.get(key, key), key),
    )
    financial_group_index = {key: index for index, key in enumerate(active_financial_groups)}
    financial_rows: list[list[int]] = []
    for (period, group_key), values in financial_aggregates.items():
        rounded = [round(value) for value in values]
        if any(rounded):
            financial_rows.append([month_index[period], financial_group_index[group_key], *rounded])
    financial_rows.sort(key=lambda item: (item[0], item[1]))
    latest_membership = {
        company: membership[0]
        for (period, company), membership in group_membership.items()
        if period == latest
    }
    company_groups = [
        financial_group_index.get(latest_membership.get(code, "c:" + code), -1)
        for code in companies
    ]

    generated = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    dataset = {
        "meta": {
            "latest": latest,
            "generated": generated,
            "source": OFFICIAL_URL,
            "sha256": source_hash,
            "balanceSha256": balance_hash,
            "rawRows": raw_rows,
            "balanceRows": balance_rows,
            "records": len(compact_rows),
            "financialRecords": len(financial_rows),
            "startYear": start_year,
            "focusCode": default_focus_code,
            "model": 4,
            "taxonomyVersion": "2026-08-financial",
            "schema": [
                "month", "company", "branch", "direct", "issued", "earned", "claims",
                "reinsuranceExpense", "reinsuranceRevenue", "commercialExpense", "rvne",
            ],
        },
        "months": months,
        "companies": [[code, company_names[code]] for code in companies],
        "branches": [[code, simplify_branch_name(branches_lookup.get(code, code), code)] for code in branches],
        "rows": compact_rows,
        "products": product_taxonomy(active_branches),
        "financial": {
            "schema": list(FINANCIAL_SCHEMA),
            "groups": [[key, group_labels.get(key, key)] for key in active_financial_groups],
            "rows": financial_rows,
            "companyGroups": company_groups,
            "formulaVersion": "2026-08-standard-combined-closing-equity-roe",
        },
    }
    if default_focus_code not in company_index:
        raise ValueError("A seguradora foco padrão não foi encontrada no recorte agregado.")
    if not {"0775", "0776"}.issubset(active_branches):
        raise ValueError("Os ramos 0775 e 0776 não foram encontrados no recorte agregado.")
    if not any(row[0] == month_index[latest] for row in financial_rows):
        raise ValueError("A base de balanço não contém dados financeiros para o período mais recente da produção.")
    return dataset


def filter_dataset(dataset: dict, branch_codes: set[str]) -> dict:
    selected_old_branches = {index for index, item in enumerate(dataset["branches"]) if item[0] in branch_codes}
    if not selected_old_branches:
        raise ValueError("Nenhum ramo solicitado foi encontrado para a versão compacta.")
    selected_rows = [row for row in dataset["rows"] if row[2] in selected_old_branches]
    selected_old_companies = sorted({row[1] for row in selected_rows})
    selected_old_branches = sorted(selected_old_branches)
    company_map = {old: new for new, old in enumerate(selected_old_companies)}
    branch_map = {old: new for new, old in enumerate(selected_old_branches)}
    compact = {
        "meta": dict(dataset["meta"], records=len(selected_rows), previewBranches=sorted(branch_codes)),
        "months": list(dataset["months"]),
        "companies": [dataset["companies"][old] for old in selected_old_companies],
        "branches": [dataset["branches"][old] for old in selected_old_branches],
        "rows": [[row[0], company_map[row[1]], branch_map[row[2]], *row[3:]] for row in selected_rows],
        "products": [],
        "financial": dataset.get("financial", {}),
    }
    available_codes = {item[0] for item in compact["branches"]}
    for product in dataset.get("products", []):
        product_copy = dict(product)
        product_copy["codes"] = [code for code in product["codes"] if code in available_codes]
        product_copy["subproducts"] = []
        for item in product.get("subproducts", []):
            item_copy = dict(item)
            item_copy["codes"] = [code for code in item["codes"] if code in available_codes]
            if item_copy["codes"]:
                product_copy["subproducts"].append(item_copy)
        if product_copy["codes"] and product_copy["subproducts"]:
            defaults = {item["id"] for item in product_copy["subproducts"]}
            if product_copy.get("defaultSubproduct") not in defaults:
                product_copy["defaultSubproduct"] = product_copy["subproducts"][0]["id"]
            compact["products"].append(product_copy)
    return compact


def build_product_preview(dataset: dict, top_per_product: int = 1) -> dict:
    """Cria um fragmento leve com todos os produtos e mercado integral.

    O preview mantém as maiores companhias de cada produto, a companhia foco e uma
    linha sintética de mercado residual (usada apenas nos totais). O ranking da
    visão inicial de Seguro Garantia mantém todas as companhias acima da
    companhia foco. No histórico, períodos não necessários à comparação corrente são
    consolidados em dezembro para preservar os totais anuais em menos de 1 MB.
    """

    latest_year = int(dataset["meta"]["latest"][:4])
    latest_month = int(dataset["meta"]["latest"][4:])
    detailed_start_year = latest_year
    month_index = {month: index for index, month in enumerate(dataset["months"])}
    branch_index = {item[0]: index for index, item in enumerate(dataset["branches"])}
    focus_index = next(index for index, item in enumerate(dataset["companies"]) if item[0] == dataset["meta"]["focusCode"])
    current_start = month_index[f"{latest_year}01"]
    current_end = month_index[f"{latest_year}{latest_month:02d}"]
    selected_companies = {focus_index}
    for product in dataset.get("products", []):
        selected_branches = {branch_index[code] for code in product["codes"] if code in branch_index}
        totals: dict[int, float] = defaultdict(float)
        for row in dataset["rows"]:
            if current_start <= row[0] <= current_end and row[2] in selected_branches:
                totals[row[1]] += row[4]
        selected_companies.update(
            company for company, _ in sorted(totals.items(), key=lambda item: item[1], reverse=True)[:top_per_product]
        )
    guarantee_branches = {branch_index[code] for code in ("0775", "0776") if code in branch_index}
    comparison_ranges = [(current_start, current_end)]
    previous_start = month_index.get(f"{latest_year - 1}01")
    previous_end = month_index.get(f"{latest_year - 1}{latest_month:02d}")
    if previous_start is not None and previous_end is not None:
        comparison_ranges.append((previous_start, previous_end))
    for range_start, range_end in comparison_ranges:
        guarantee_totals: dict[int, float] = defaultdict(float)
        for row in dataset["rows"]:
            if range_start <= row[0] <= range_end and row[2] in guarantee_branches:
                guarantee_totals[row[1]] += row[4]
        focus_guarantee = guarantee_totals.get(focus_index, 0)
        selected_companies.update(company for company, value in guarantee_totals.items() if value > focus_guarantee)

    old_companies = sorted(selected_companies)
    company_map = {old: new for new, old in enumerate(old_companies)}
    residual_company = len(old_companies)
    aggregates: dict[tuple[int, int, int], list[int]] = defaultdict(lambda: [0] * len(MEASURES))
    for row in dataset["rows"]:
        period = dataset["months"][row[0]]
        year = int(period[:4])
        month = int(period[4:])
        if year < latest_year - 1 or (year == latest_year - 1 and month > latest_month):
            compact_month = month_index[f"{year}12"]
        else:
            compact_month = row[0]
        compact_company = company_map.get(row[1], residual_company)
        values = aggregates[(compact_month, compact_company, row[2])]
        for index, value in enumerate(row[3:11]):
            values[index] += value
    rows = [
        [month, company, branch, *values]
        for (month, company, branch), values in aggregates.items()
        if any(values)
    ]
    rows.sort(key=lambda item: (item[0], item[1], item[2]))
    companies = [dataset["companies"][old] for old in old_companies]
    companies.append(["__MARKET__", "Demais mercado (agregado)"])
    financial = dataset.get("financial", {})
    financial_months = {current_end}
    if previous_end is not None:
        financial_months.add(previous_end)
    preview_financial = {
        "schema": list(financial.get("schema", [])),
        "groups": list(financial.get("groups", [])),
        "rows": [row for row in financial.get("rows", []) if row[0] in financial_months],
        "companyGroups": [financial.get("companyGroups", [])[old] for old in old_companies]
        + [-1],
        "formulaVersion": financial.get("formulaVersion"),
        "previewMonths": sorted(financial_months),
    }
    return {
        "meta": dict(
            dataset["meta"],
            records=len(rows),
            preview=True,
            previewDetailedStartYear=detailed_start_year,
            previewTopPerProduct=top_per_product,
        ),
        "months": list(dataset["months"]),
        "companies": companies,
        "branches": list(dataset["branches"]),
        "rows": rows,
        "products": dataset.get("products", []),
        "financial": preview_financial,
    }


def standalone_wrapper(fragment: str) -> str:
    return """<!doctype html>
<html lang=\"pt-BR\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Radar do Mercado Segurador Brasileiro | Dados públicos SUSEP</title>
<style>
:root{color-scheme:light dark;--background:light-dark(#f4f7fb,#07111f);--foreground:light-dark(#142033,#f3f6fb);--card:light-dark(#fff,#0c1a2b);--card-foreground:var(--foreground);--muted:light-dark(#e8edf5,#15263a);--muted-foreground:light-dark(#5d6b7d,#9fb0c5);--border:light-dark(#d8e0ea,#26394f);--input:var(--border);--ring:light-dark(#284a6d,#79b7e8);--primary:light-dark(#183b5b,#d9e9f7);--primary-foreground:light-dark(#fff,#0b1726);--accent:light-dark(#e7f0f7,#17314a);--accent-foreground:var(--foreground);--green:light-dark(#16744a,#65d7a1);--red:light-dark(#b33a43,#ff8791);--viz-series-1:light-dark(#003b70,#6ea8d8);--viz-series-2:light-dark(#246b9b,#76b8e3);--viz-series-3:light-dark(#597b9c,#86a9c9);--viz-series-4:light-dark(#537a63,#78c495);--viz-series-5:light-dark(#7c5d9c,#b99ae1);--viz-series-6:light-dark(#8b6b49,#d2aa7c);font-family:Inter,Segoe UI,Arial,sans-serif;--font-size-base:16px}
*{box-sizing:border-box}body{margin:0;background:var(--background);color:var(--foreground);font-size:var(--font-size-base)}main{max-width:1480px;margin:auto;padding:28px clamp(16px,3vw,48px) 44px}.card{background:var(--card);color:var(--card-foreground);border:1px solid var(--border);border-radius:12px;padding:18px}.viz-grid{display:grid;gap:14px}.viz-stat-value{font-size:clamp(1.35rem,2.6vw,2.05rem);font-weight:500;margin-top:8px}.text-small{font-size:.82rem}.text-muted{color:var(--muted-foreground)}.form-label{display:block}.form-select,.form-control{width:100%;font:inherit;color:var(--foreground);background:var(--card);border:1px solid var(--input);border-radius:8px;padding:9px 10px}.form-range{width:100%}.viz-controls{display:flex;gap:12px;flex-wrap:wrap}.btn{font:inherit;color:var(--foreground);background:var(--card);border:1px solid var(--border);border-radius:8px;padding:9px 13px;cursor:pointer}.btn-primary{background:var(--primary);color:var(--primary-foreground)}h1,h2,h3{font-weight:500}button,select,input{accent-color:var(--viz-series-1)}
</style>
</head>
<body><main>""" + fragment + """</main></body>
</html>
"""


def render_outputs(
    dataset: dict,
    fragment_dataset: dict,
    template_path: Path,
    json_output: Path,
    fragment_output: Path,
    html_output: Path,
) -> None:
    template = template_path.read_text(encoding="utf-8")
    token = "__SES_DATA__"
    if template.count(token) != 1:
        raise ValueError(f"O template deve conter exatamente uma ocorrência de {token}.")
    compact = json.dumps(dataset, ensure_ascii=False, separators=(",", ":"))
    fragment_compact = json.dumps(fragment_dataset, ensure_ascii=False, separators=(",", ":"))
    fragment = template.replace(token, fragment_compact)
    standalone = template.replace(token, compact)
    atomic_write_text(json_output, compact)
    atomic_write_text(fragment_output, fragment)
    atomic_write_text(html_output, standalone_wrapper(standalone))


def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Atualiza o Radar do Mercado Segurador com rankings financeiros.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path, help="ZIP local com os arquivos SES.")
    source.add_argument("--download", action="store_true", help="Baixa a BaseCompleta.zip oficial.")
    parser.add_argument(
        "--balance-source",
        type=Path,
        help="ZIP separado com Ses_Balanco.csv. Se omitido, o arquivo é lido do ZIP principal.",
    )
    parser.add_argument("--url", default=OFFICIAL_URL, help="URL de download da Base Completa.")
    parser.add_argument("--cache-dir", type=Path, default=here / "cache")
    parser.add_argument("--start-year", type=int, default=2019)
    parser.add_argument("--template", type=Path, default=here / "dashboard-v4.fragment.template.html")
    parser.add_argument("--json-output", type=Path, default=here / "dist" / "market.json")
    parser.add_argument("--fragment-output", type=Path, default=here / "dist" / "dashboard-fragment.html")
    parser.add_argument("--html-output", type=Path, default=here / "dist" / "index.html")
    parser.add_argument("--preview-top", type=int, default=1, help="Maiores companhias por produto mantidas no preview compacto.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = download_base(args.url, args.cache_dir) if args.download else args.source.resolve()
    if args.balance_source:
        balance_source = args.balance_source.resolve()
    else:
        with zipfile.ZipFile(source) as archive:
            balance_source = source if "ses_balanco.csv" in archive_members(archive) else None
        if balance_source is None:
            print("ERRO: informe --balance-source com o ZIP que contém Ses_Balanco.csv.", file=sys.stderr)
            return 1
    try:
        dataset = build_dataset(source, balance_source, args.start_year)
        fragment_dataset = build_product_preview(dataset, max(1, args.preview_top))
        render_outputs(dataset, fragment_dataset, args.template, args.json_output, args.fragment_output, args.html_output)
    except Exception as error:
        print(f"ERRO: atualização não concluída; os últimos arquivos válidos foram preservados. {error}", file=sys.stderr)
        return 1
    meta = dataset["meta"]
    print(
        f"OK: base até {meta['latest']} | {meta['records']:,} registros de produção | "
        f"{meta['financialRecords']:,} registros financeiros | "
        f"dashboard: {args.html_output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
