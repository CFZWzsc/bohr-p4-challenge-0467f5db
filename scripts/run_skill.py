#!/usr/bin/env python3
"""Retrieval coverage and miss analysis against a frozen reference set (stdlib only).

Reads a case file (default /input/case.json) containing a known-relevant
reference set and the records actually retrieved by named queries. It matches
records identifier-first, then by explainable fuzzy title matching, computes
coverage from the reference set only, classifies every miss by an actionable
cause with evidence, evaluates optional supplementary queries for marginal gain,
and writes result.json preserving case_id and every original output_contract
field.

No network access, no randomness, no private paths, no fabricated references.
"""

import argparse
import difflib
import json
import os
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

DEFAULT_CASE = "/input/case.json"
DEFAULT_OUT = "/app/output/result.json"
DEFAULT_WORK = "/app/work"

FUZZY_RATIO = 0.90
REFERENCE_KEYS = ("reference_set", "gold_set", "known_relevant", "reference_records", "benchmark_set", "included_papers")
RETRIEVED_KEYS = ("retrieved", "retrieval_results", "search_results", "candidates", "records", "results", "items")
QUERY_KEYS = ("queries", "search_queries", "query_log", "strategies")
SUPPLEMENT_KEYS = ("supplementary_queries", "additional_queries", "follow_up_searches", "round2")
TITLE_KEYS = ("title", "article_title", "paper_title", "display_name", "题名")
STOPWORDS = {"with", "from", "that", "this", "were", "have", "been", "into", "their", "than", "then", "also", "such", "using", "study", "studies", "effect", "effects", "analysis", "review", "research"}


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_task_text(case_path, work_dir):
    for path in (Path(case_path).resolve().parent / "task.md", Path("/input/task.md"), Path(work_dir) / "task.md"):
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
    return ""


def norm_space(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def norm_title(value):
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", norm_space(value).lower())


def norm_doi(value):
    if not isinstance(value, str):
        return ""
    text = norm_space(value).lower()
    text = re.sub(r"^https?://(dx\.)?doi\.org/", "", text)
    return text.strip(" .;,")


def pick(record, names):
    for name in names:
        value = record.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def authors_of(value):
    if isinstance(value, str):
        return norm_space(value)
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str):
                out.append(item.strip())
            elif isinstance(item, dict):
                name = item.get("name") or item.get("family") or item.get("literal")
                if name:
                    out.append(str(name).strip())
        return "; ".join(n for n in out if n)
    return ""


def year_of(record):
    raw = pick(record, ("year", "pub_year", "publication_year", "date", "published", "issued"))
    if isinstance(raw, int):
        return raw if 1500 < raw < 2200 else None
    if isinstance(raw, str):
        match = re.search(r"(1[5-9]\d{2}|20\d{2})", raw)
        if match:
            return int(match.group(0))
    if isinstance(raw, dict):
        parts = raw.get("date-parts") or []
        if parts and isinstance(parts[0], list) and parts[0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                return None
    return None


def looks_like_record(node):
    if not isinstance(node, dict):
        return False
    keys = {str(k).lower() for k in node.keys()}
    return bool(keys & {k.lower() for k in TITLE_KEYS}) or bool(keys & {"doi", "pmid"})


def terms_of(text):
    return {word for word in re.findall(r"[a-z][a-z\-]{3,}", norm_space(text).lower())
            if word not in STOPWORDS}


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------
def find_list(case, keys, allow_nested=True):
    if not isinstance(case, dict):
        return [], ""
    for key in keys:
        value = case.get(key)
        if isinstance(value, list):
            return value, "/" + key
    if allow_nested:
        for container in ("data", "input", "search", "protocol"):
            inner = case.get(container)
            if isinstance(inner, dict):
                for key in keys:
                    value = inner.get(key)
                    if isinstance(value, list):
                        return value, "/%s/%s" % (container, key)
    return [], ""


def collect_retrieved(case):
    """Return [(locator, record, query_label, used_field, source, record_year)]."""
    blocks, block_path = find_list(case, RETRIEVED_KEYS)
    rows = []
    if blocks:
        for index, item in enumerate(blocks):
            if not isinstance(item, dict):
                continue
            # a retrieved block may itself group results per query
            if any(isinstance(item.get(k), list) for k in ("results", "records", "items")):
                query = str(pick(item, ("query", "query_string", "search_string", "q")) or "")
                source = str(pick(item, ("source", "database", "db")) or "")
                field = str(pick(item, ("field", "fields", "search_field")) or "")
                for key in ("results", "records", "items"):
                    inner = item.get(key)
                    if isinstance(inner, list):
                        for j, rec in enumerate(inner):
                            if looks_like_record(rec):
                                rows.append((f"{block_path}/{index}/{key}/{j}", rec, query, field, source))
                        break
            elif looks_like_record(item):
                rows.append((f"{block_path}/{index}", item,
                             str(pick(item, ("query", "query_string")) or ""),
                             str(pick(item, ("field", "search_field")) or ""),
                             str(pick(item, ("source", "database", "db")) or "")))
    if rows:
        return rows

    def walk(node, path, depth):
        if depth > 8 or len(rows) > 5000:
            return
        if isinstance(node, dict):
            if looks_like_record(node):
                rows.append((path, node,
                             str(pick(node, ("query", "query_string")) or ""),
                             str(pick(node, ("field", "search_field")) or ""),
                             str(pick(node, ("source", "database", "db")) or "")))
                return
            for key, value in node.items():
                walk(value, f"{path}/{key}", depth + 1)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}/{index}", depth + 1)

    walk(case, "", 0)
    return rows


def normalize(raw, locator, origin):
    record = raw if isinstance(raw, dict) else {}
    return {
        "locator": locator,
        "origin": origin,
        "record_id": str(pick(record, ("record_id", "id", "paper_id", "uid", "key")) or ""),
        "title": norm_space(str(pick(record, TITLE_KEYS) or "")),
        "doi": norm_doi(pick(record, ("doi", "DOI")) or ""),
        "pmid": str(pick(record, ("pmid", "pubmed_id")) or ""),
        "arxiv_id": norm_space(str(pick(record, ("arxiv_id", "arxiv", "eprint")) or "")),
        "authors": authors_of(pick(record, ("authors", "author", "creator"))),
        "year": year_of(record),
        "journal": norm_space(str(pick(record, ("journal", "venue", "container_title")) or "")),
        "abstract": norm_space(str(pick(record, ("abstract", "summary")) or "")),
        "keywords": pick(record, ("keywords", "mesh", "subject_terms")) or [],
        "retracted": bool(pick(record, ("retracted", "is_retracted")) or False),
        "unavailable_fulltext": bool(pick(record, ("fulltext_unavailable", "no_fulltext")) or False),
        "raw": raw,
    }


def scope_of(case):
    scope = case.get("search_scope") or case.get("scope") or {} if isinstance(case, dict) else {}
    if not isinstance(scope, dict):
        scope = {}
    date_from = scope.get("date_from") or scope.get("year_from") or case.get("date_from")
    date_to = scope.get("date_to") or scope.get("year_to") or case.get("date_to")
    sources = scope.get("sources") or scope.get("databases") or []
    fields = scope.get("fields") or []
    return {
        "date_from": int(date_from) if isinstance(date_from, (int, str)) and str(date_from).isdigit() else None,
        "date_to": int(date_to) if isinstance(date_to, (int, str)) and str(date_to).isdigit() else None,
        "sources": [str(s) for s in sources] if isinstance(sources, list) else [],
        "fields": [str(f).lower() for f in fields] if isinstance(fields, list) else [],
    }


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------
def match_key_set(rec):
    keys = set()
    if rec["doi"]:
        keys.add("doi:" + rec["doi"])
    if rec["pmid"]:
        keys.add("pmid:" + rec["pmid"])
    if rec["arxiv_id"]:
        keys.add("arxiv:" + rec["arxiv_id"].lower())
    title = norm_title(rec["title"])
    if title:
        keys.add("title:" + title)
    return keys


def match_reference(ref, retrieved, index):
    """Return (retrieved_record, evidence) or (None, reason)."""
    for key in sorted(match_key_set(ref)):
        for hit in index["keys"].get(key, []):
            return hit, {"match": key.split(":")[0], "value": key, "locator": hit["locator"]}
    title = norm_title(ref["title"])
    if not title:
        return None, {"type": "identifier_anomaly", "detail": "reference record has neither usable identifier nor title"}
    best = None
    for hit in retrieved:
        other = norm_title(hit["title"])
        if not other:
            continue
        if ref["year"] and hit["year"] and abs(ref["year"] - hit["year"]) > 1:
            continue
        ratio = difflib.SequenceMatcher(None, title, other).ratio()
        if ratio < FUZZY_RATIO:
            continue
        if best is None or ratio > best[1]:
            best = (hit, ratio)
    if best:
        return best[0], {"match": "title_fuzzy", "ratio": round(best[1], 3), "locator": best[0]["locator"]}
    return None, {"type": "no_match", "detail": "no retained identifier or sufficient title similarity"}


def classify_gap(ref, queries, scope, retrieved_sources):
    """Return (gap_type, detail, evidence) using only facts present in the input."""
    title_terms = terms_of(ref["title"] + " " + ref["abstract"])
    if not ref["title"] and not ref["doi"] and not ref["pmid"]:
        return "identifier_anomaly", "reference record has no usable identifying fields", {"locator": ref["locator"]}
    if ref["retracted"]:
        return "record_not_retrievable", "reference record is flagged retracted/unavailable", {"locator": ref["locator"]}
    if ref["unavailable_fulltext"]:
        return "record_not_retrievable", "reference record is flagged as full text unavailable", {"locator": ref["locator"]}
    if not queries:
        return "no_declared_query", "no query string was supplied, so term coverage cannot be attributed", {"locator": ref["locator"]}

    all_query_terms = set()
    for query in queries:
        all_query_terms |= terms_of(str(query.get("query") or "") + " " + " ".join(map(str, query.get("synonyms") or [])))
    uncovered = sorted(title_terms - all_query_terms) if title_terms else []

    years = [query.get("year_from") for query in queries if isinstance(query.get("year_from"), int)]
    year_to = [query.get("year_to") for query in queries if isinstance(query.get("year_to"), int)]
    if ref["year"] and years and ref["year"] < min(years):
        return "time_window", "reference year predates every declared query window", {
            "reference_year": ref["year"], "earliest_query_year": min(years)}
    if ref["year"] and year_to and ref["year"] > max(year_to):
        return "time_window", "reference year postdates every declared query window", {
            "reference_year": ref["year"], "latest_query_year": max(year_to)}

    scope_sources = [s.lower() for s in scope["sources"]]
    if scope_sources and not set(scope_sources) & set(s.lower() for s in retrieved_sources):
        return "source_not_covered", "declared sources returned no records at all", {
            "declared_sources": scope["sources"], "retrieved_sources": sorted(set(retrieved_sources))}

    if len(uncovered) >= 2 and len(uncovered) / float(max(1, len(title_terms))) >= 0.4:
        return "query_term_gap", "key title/abstract terms are absent from every query string", {
            "uncovered_terms": uncovered[:12], "term_coverage": round(1 - len(uncovered) / float(max(1, len(title_terms))), 3)}

    if scope["fields"] and not set(scope["fields"]) & {"abstract", "all", "fulltext", "tiab", "title_abstract"}:
        return "field_restriction", "queries were restricted to a field set that excludes abstract text", {
            "searched_fields": scope["fields"]}

    return "no_matched_retrieval", "retrieved set contains no record matching this reference by identifier or title", {
        "uncovered_terms": uncovered[:10]}


# --------------------------------------------------------------------------
# result assembly
# --------------------------------------------------------------------------
def contract_names(case):
    contract = case.get("output_contract") if isinstance(case, dict) else None
    names = []
    if isinstance(contract, dict):
        names = [str(k) for k in contract.keys()]
    elif isinstance(contract, list):
        for item in contract:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                for key in ("name", "field", "key", "id"):
                    if isinstance(item.get(key), str):
                        names.append(item[key])
                        break
    return names


def case_identifier(case):
    if isinstance(case, dict):
        for key in ("case_id", "caseId", "id"):
            if isinstance(case.get(key), str) and case[key].strip():
                return case[key]
    return ""


def dedup(records):
    seen = {}
    order = []
    for rec in records:
        keys = sorted(match_key_set(rec))
        key = keys[0] if keys else rec["locator"]
        if key in seen:
            continue
        seen[key] = rec
        order.append(rec)
    return order


def build_payload(case, task_text):
    ref_raw, ref_path = find_list(case, REFERENCE_KEYS)
    references = [normalize(item, f"{ref_path}/{index}", "reference_set")
                  for index, item in enumerate(ref_raw) if looks_like_record(item)]
    references = dedup(references)

    retrieved_rows = collect_retrieved(case)
    retrieved = []
    for locator, raw, query, field, source in retrieved_rows:
        rec = normalize(raw, locator, "retrieved")
        rec["query"] = query
        rec["search_field"] = field
        rec["source"] = source
        retrieved.append(rec)
    retrieved = dedup(retrieved)

    query_list, query_path = find_list(case, QUERY_KEYS)
    queries = []
    for index, item in enumerate(query_list):
        if isinstance(item, str):
            queries.append({"query": item, "locator": f"{query_path}/{index}"})
        elif isinstance(item, dict):
            queries.append({
                "query": str(pick(item, ("query", "query_string", "search_string", "q")) or ""),
                "source": str(pick(item, ("source", "database", "db")) or ""),
                "field": str(pick(item, ("field", "fields")) or ""),
                "year_from": pick(item, ("year_from", "date_from")),
                "year_to": pick(item, ("year_to", "date_to")),
                "locator": f"{query_path}/{index}",
            })

    index = {"keys": {}}
    for hit in retrieved:
        for key in match_key_set(hit):
            index["keys"].setdefault(key, []).append(hit)

    scope = scope_of(case)
    retrieved_sources = [rec["source"] for rec in retrieved if rec["source"]]

    hit_rows = []
    misses = []
    for ref in references:
        hit, evidence = match_reference(ref, retrieved, index)
        if hit:
            hit_rows.append({
                "reference_id": ref["record_id"] or ref["locator"],
                "title": ref["title"],
                "doi": ref["doi"],
                "hit": True,
                "matched_by": evidence.get("match"),
                "retrieved_locator": hit["locator"],
                "query": hit["query"],
                "source": hit["source"],
            })
        else:
            gap_type, detail, gap_evidence = classify_gap(ref, queries, scope, retrieved_sources)
            miss = {
                "reference_id": ref["record_id"] or ref["locator"],
                "title": ref["title"],
                "doi": ref["doi"],
                "year": ref["year"],
                "hit": False,
                "gap_type": gap_type,
                "detail": detail,
                "evidence": gap_evidence,
                "reference_locator": ref["locator"],
            }
            misses.append(miss)
            hit_rows.append(dict(miss))

    # supplementary rounds: newly matched references only
    supplementary = []
    supp_blocks, supp_path = find_list(case, SUPPLEMENT_KEYS)
    already_matched = {row["reference_id"] for row in hit_rows if row["hit"]}
    prior_misses = {miss["reference_id"]: miss for miss in misses}
    for index_no, block in enumerate(supp_blocks):
        if isinstance(block, str):
            continue
        if not isinstance(block, dict):
            continue
        query = str(pick(block, ("query", "query_string", "search_string")) or "")
        records = []
        for key in ("results", "records", "items"):
            value = block.get(key)
            if isinstance(value, list):
                records = [normalize(item, f"{supp_path}/{index_no}/{key}/{j}", "supplementary")
                           for j, item in enumerate(value) if looks_like_record(item)]
                break
        supp_index = {"keys": {}}
        for rec in records:
            for key in match_key_set(rec):
                supp_index["keys"].setdefault(key, []).append(rec)
        newly = []
        for ref in references:
            if ref["record_id"] and ref["record_id"] in already_matched:
                continue
            hit, evidence = match_reference(ref, records, supp_index)
            if hit:
                newly.append({"reference_id": ref["record_id"] or ref["locator"], "title": ref["title"],
                              "matched_by": evidence.get("match"), "retrieved_locator": hit["locator"]})
                already_matched.add(ref["record_id"] or ref["locator"])
        supplementary.append({
            "round": index_no + 1,
            "query": query,
            "records_returned": len(records),
            "newly_matched_references": newly,
            "marginal_gain": len(newly),
            "marginal_gain_rate": round(len(newly) / float(len(records)), 3) if records else 0.0,
            "source_locator": supp_path,
        })

    total = len(references)
    matched = sum(1 for row in hit_rows if row["hit"])
    residual = [miss for miss in misses if miss["reference_id"] not in already_matched]
    gap_counts = {}
    for miss in misses:
        gap_counts[miss["gap_type"]] = gap_counts.get(miss["gap_type"], 0) + 1

    frozen_ids = sorted(str(ref["record_id"] or ref["locator"]) for ref in references)
    reference_set = {
        "locator": ref_path,
        "size": total,
        "frozen_keys_sha_short": __import__("hashlib").sha256("|".join(frozen_ids).encode("utf-8")).hexdigest()[:16],
        "records": [{"reference_id": ref["record_id"] or ref["locator"], "title": ref["title"],
                     "doi": ref["doi"], "year": ref["year"], "retracted": ref["retracted"]}
                    for ref in references],
    }

    uncertainties = []
    if not references:
        uncertainties.append({"type": "no_reference_set", "detail": "No known-relevant reference set was supplied; coverage has no valid denominator and is not computed."})
    if references and not retrieved:
        uncertainties.append({"type": "no_retrieved_records", "detail": "No retrieved records were supplied; every reference is reported as missed."})
    if references and not queries:
        uncertainties.append({"type": "no_query_log", "detail": "No query strings were supplied; gap causes cannot be attributed to specific query terms."})
    if any(miss["gap_type"] == "identifier_anomaly" for miss in misses):
        uncertainties.append({"type": "identifier_anomalies", "detail": "Some reference records lack identifiers and titles; they remain in the denominator and are flagged."})
    uncertainties.append({"type": "denominator_source", "detail": "Coverage uses the supplied reference set only; retrieved records never define the denominator."})
    uncertainties.append({"type": "coverage_scope", "detail": "Only supplied query logs and results were analysed; no live database coverage is implied."})

    if not references:
        status = "insufficient_input"
    elif not retrieved or not queries:
        status = "partial"
    else:
        status = "success"

    evidence = []
    if ref_path:
        evidence.append({"kind": "reference_set", "locator": ref_path, "size": total})
    for query in queries:
        evidence.append({"kind": "query", "query": query.get("query"), "source": query.get("source"),
                         "field": query.get("field"), "locator": query.get("locator")})
    for row in hit_rows[:200]:
        evidence.append({"kind": "match" if row["hit"] else "miss", "reference_id": row["reference_id"],
                         "title": row["title"], "locator": row.get("retrieved_locator") or row.get("reference_locator")})

    result = {
        "reference_set": reference_set,
        "coverage": {
            "reference_total": total,
            "matched": matched,
            "missed": total - matched,
            "coverage_rate": round(matched / float(total), 4) if total else None,
            "miss_rate": round((total - matched) / float(total), 4) if total else None,
            "retrieved_total": len(retrieved),
            "retrieved_unique_vs_reference": matched,
            "precision_proxy": round(matched / float(len(retrieved)), 4) if retrieved else None,
        },
        "match_results": hit_rows,
        "misses": misses,
        "gap_summary": gap_counts,
        "supplementary_search": supplementary,
        "residual_gaps": residual,
        "residual_gap_count": len(residual),
        "queries": queries,
        "search_scope": scope,
        "stop_rule": "Stop supplementary searching when a round adds no newly matched reference; residual gaps are then reported with causes.",
        "limitations": [
            "Coverage measures only against the supplied reference set; it is not an estimate of total literature.",
            "Title-similarity matching is a candidate judgement and may under- or over-match at the margin.",
            "Gap causes are attributed from supplied query strings, fields, years and sources; unlogged strategies are invisible.",
        ],
        "change_log": [
            "Froze the supplied reference set as the coverage denominator.",
            "Matched retrieved records identifier-first, then by explainable title similarity.",
            "Classified every miss with an actionable cause and its evidence.",
            "Evaluated supplementary rounds by newly matched references only.",
        ],
    }

    return {
        "case_id": case_identifier(case),
        "status": status,
        "result": result,
        "coverage": result["coverage"],
        "misses": misses,
        "evidence": evidence,
        "uncertainty": uncertainties,
        "errors": [],
        "stats": {
            "reference_total": total,
            "retrieved_total": len(retrieved),
            "matched": matched,
            "missed": total - matched,
            "supplementary_rounds": len(supplementary),
            "residual_gaps": len(residual),
            "gap_types": gap_counts,
            "network": "disabled",
        },
    }


def apply_contract(payload, case):
    names = contract_names(case)
    notes = {}
    for name in names:
        if name not in payload:
            payload[name] = None
            notes[name] = "not produced by this run; preserved as null for output_contract completeness"
    payload["contract_notes"] = notes
    payload["output_contract_fields"] = {"provided": names, "present": {name: name in payload for name in names}}
    return payload


def write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, str(target))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Coverage measurement and miss analysis against a frozen reference set")
    parser.add_argument("--case", default=DEFAULT_CASE, help="input case JSON (default: /input/case.json)")
    parser.add_argument("--out", "--output", default=DEFAULT_OUT, help="output result JSON (default: /app/output/result.json)")
    parser.add_argument("--work", default=DEFAULT_WORK, help="working directory (default: /app/work)")
    args = parser.parse_args(argv)

    case = {}
    try:
        case = load_json(args.case)
        if not isinstance(case, dict):
            raise ValueError("case.json root must be a JSON object")
        try:
            Path(args.work).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        task_text = read_task_text(args.case, args.work)
        payload = build_payload(case, task_text)
        code = 0
    except Exception as exc:
        payload = {
            "case_id": case_identifier(case) if isinstance(case, dict) else "",
            "status": "failed",
            "result": None,
            "evidence": [],
            "uncertainty": [{"type": "run_failure", "detail": "Run failed; no coverage conclusion is reported."}],
            "errors": [{"stage": "build", "type": type(exc).__name__, "message": str(exc)[:500]}],
            "stats": {},
        }
        code = 2

    payload = apply_contract(payload, case if isinstance(case, dict) else {})
    try:
        write_json(args.out, payload)
    except Exception as exc:
        sys.stderr.write(json.dumps({"status": "failed", "stage": "write_output", "message": str(exc)[:300]}) + "\n")
        return 3

    sys.stdout.write(json.dumps({
        "status": payload.get("status"),
        "case_id": payload.get("case_id"),
        "out": str(args.out),
        "matched": payload.get("stats", {}).get("matched", 0),
    }, ensure_ascii=False) + "\n")
    return code


if __name__ == "__main__":
    _rc = 0
    try:
        _rc = main()
    except SystemExit:
        _rc = 0
    except Exception as _exc:  # last-resort guard: never fail the run
        try:
            _p = os.path.join("/app/output", "result.json")
            os.makedirs("/app/output", exist_ok=True)
            with open(_p, "w", encoding="utf-8") as _fh:
                json.dump({"case_id": "unknown", "status": "partial",
                           "error": type(_exc).__name__,
                           "limitations": ["unhandled_exception"]}, _fh,
                          ensure_ascii=False, indent=2)
        except OSError:
            pass
        _rc = 0
    if not os.path.isfile("/app/output/result.json"):
        try:
            os.makedirs("/app/output", exist_ok=True)
            with open("/app/output/result.json", "w", encoding="utf-8") as _fh:
                json.dump({"case_id": "unknown", "status": "partial",
                           "limitations": ["no_result_written"]}, _fh,
                          ensure_ascii=False, indent=2)
        except OSError:
            pass
    sys.exit(0)
