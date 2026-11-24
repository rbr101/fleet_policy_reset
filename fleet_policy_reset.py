#!/usr/bin/env python3
"""
Verwijdert een (gepreconfigureerde) Fleet agent policy én het bijbehorende
fleet-preconfiguration-deletion-record, zodat Kibana de policy bij de
volgende herstart opnieuw aanmaakt vanuit de ECK/Kibana-config.

Stappen:
  1. Tijdelijke rol + user aanmaken (rechten op .kibana_ingest*)
  2. Agent policy verwijderen via de Fleet API
  3. Deletion record opzoeken in .kibana_ingest
  4. Deletion record verwijderen (als tijdelijke user)
  5. Tijdelijke rol + user opruimen (altijd, ook bij fouten)

Gebruik:
  python3 fleet_policy_reset.py                 # gebruikt config.ini
  python3 fleet_policy_reset.py -c andere.ini
  python3 fleet_policy_reset.py --policy-id Kafka-consumer --force
  python3 fleet_policy_reset.py --dry-run       # alleen tonen, niets wijzigen
"""

import argparse
import configparser
import getpass
import secrets
import string
import sys
import time
from urllib.parse import quote

import requests

TEMP_ROLE = "kibana_ingest_admin"
TEMP_USER = "tmp_ingest_admin"
RECORD_TYPE = "fleet-preconfiguration-deletion-record"
TIMEOUT = 30

ROLE_BODY = {
    "indices": [
        {
            "names": [".kibana_ingest*"],
            "privileges": ["read", "write", "delete", "view_index_metadata"],
            "allow_restricted_indices": True,
        }
    ]
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(f"[+] {msg}")


def warn(msg: str) -> None:
    print(f"[!] {msg}", file=sys.stderr)


def load_config(path: str) -> dict:
    cp = configparser.ConfigParser()
    if not cp.read(path):
        sys.exit(f"Config-bestand niet gevonden: {path}")
    if "elastic" not in cp:
        sys.exit(f"Sectie [elastic] ontbreekt in {path}")
    s = cp["elastic"]

    verify_raw = s.get("verify_ssl", "true").strip()
    if verify_raw.lower() in ("false", "no", "0", "off"):
        verify = False
    elif verify_raw.lower() in ("true", "yes", "1", "on"):
        verify = True
    else:
        verify = verify_raw  # pad naar CA-bestand

    try:
        return {
            "es_url": s["elasticsearch_url"].rstrip("/"),
            "kibana_url": s["kibana_url"].rstrip("/"),
            "username": s["username"],
            "policy_id": s.get("policy_id", "").strip(),
            "verify": verify,
        }
    except KeyError as e:
        sys.exit(f"Verplichte instelling ontbreekt in [elastic]: {e}")


def gen_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits + "-_.!"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def make_session(user: str, password: str, verify) -> requests.Session:
    s = requests.Session()
    s.auth = (user, password)
    s.verify = verify
    s.headers.update({"Content-Type": "application/json", "kbn-xsrf": "true"})
    return s


def check(resp: requests.Response, what: str, ok=(200, 201)) -> dict:
    if resp.status_code not in ok:
        raise RuntimeError(f"{what} mislukt: HTTP {resp.status_code} - {resp.text}")
    try:
        return resp.json()
    except ValueError:
        return {}


# --------------------------------------------------------------------------- #
# Stappen
# --------------------------------------------------------------------------- #
def verify_login(admin: requests.Session, cfg: dict) -> None:
    r = admin.get(f"{cfg['es_url']}/_security/_authenticate", timeout=TIMEOUT)
    data = check(r, "Inloggen op Elasticsearch")
    log(f"Ingelogd op Elasticsearch als '{data.get('username')}'")
    r = admin.get(f"{cfg['kibana_url']}/api/status", timeout=TIMEOUT)
    check(r, "Verbinden met Kibana")
    log("Verbinding met Kibana OK")


def create_temp_role_user(admin: requests.Session, cfg: dict, temp_pw: str) -> None:
    r = admin.put(f"{cfg['es_url']}/_security/role/{TEMP_ROLE}",
                  json=ROLE_BODY, timeout=TIMEOUT)
    check(r, f"Rol '{TEMP_ROLE}' aanmaken")
    log(f"Tijdelijke rol '{TEMP_ROLE}' aangemaakt")

    r = admin.post(f"{cfg['es_url']}/_security/user/{TEMP_USER}",
                   json={"password": temp_pw, "roles": [TEMP_ROLE],
                         "full_name": "Tijdelijke user fleet_policy_reset.py"},
                   timeout=TIMEOUT)
    check(r, f"User '{TEMP_USER}' aanmaken")
    log(f"Tijdelijke user '{TEMP_USER}' aangemaakt")


def delete_policy(admin: requests.Session, cfg: dict, policy_id: str, force: bool) -> None:
    r = admin.get(f"{cfg['kibana_url']}/api/fleet/agent_policies/{quote(policy_id, safe='')}",
                  timeout=TIMEOUT)
    if r.status_code == 404:
        warn(f"Policy '{policy_id}' bestaat niet (meer) - ga door met opruimen record")
        return
    data = check(r, f"Policy '{policy_id}' ophalen").get("item", {})
    agents = data.get("agents", 0)
    log(f"Policy gevonden: '{data.get('name')}' (agents: {agents}, "
        f"managed: {data.get('is_managed')}, preconfigured: {data.get('is_preconfigured')})")
    if agents and not force:
        raise RuntimeError(
            f"Er zijn nog {agents} agent(s) aan deze policy gekoppeld. "
            "Unenroll/reassign ze eerst, of gebruik --force.")

    body = {"agentPolicyId": policy_id}
    if force:
        body["force"] = True
    r = admin.post(f"{cfg['kibana_url']}/api/fleet/agent_policies/delete",
                   json=body, timeout=TIMEOUT)
    check(r, f"Policy '{policy_id}' verwijderen")
    log(f"Policy '{policy_id}' verwijderd")


def find_records(temp: requests.Session, cfg: dict, policy_id: str) -> list:
    query = {"size": 100, "query": {"term": {"type": RECORD_TYPE}}}
    # Record kan een moment nodig hebben voordat het doorzoekbaar is
    for attempt in range(5):
        r = temp.post(f"{cfg['es_url']}/.kibana_ingest/_search?filter_path=hits.hits._id,hits.hits._source",
                      json=query, timeout=TIMEOUT)
        hits = check(r, "Deletion records opzoeken").get("hits", {}).get("hits", [])
        matches = [h["_id"] for h in hits
                   if h.get("_source", {}).get(RECORD_TYPE, {}).get("id") == policy_id]
        if matches:
            return matches
        if attempt < 4:
            time.sleep(2)
    return []


def delete_records(temp: requests.Session, cfg: dict, ids: list) -> None:
    for doc_id in ids:
        url = f"{cfg['es_url']}/.kibana_ingest/_doc/{quote(doc_id, safe='')}?refresh=true"
        r = temp.delete(url, timeout=TIMEOUT)
        data = check(r, f"Record '{doc_id}' verwijderen", ok=(200, 404))
        log(f"Record '{doc_id}': {data.get('result', r.status_code)}")


def cleanup(admin: requests.Session, cfg: dict) -> None:
    for what, url in (
        (f"user '{TEMP_USER}'", f"{cfg['es_url']}/_security/user/{TEMP_USER}"),
        (f"rol '{TEMP_ROLE}'", f"{cfg['es_url']}/_security/role/{TEMP_ROLE}"),
    ):
        try:
            r = admin.delete(url, timeout=TIMEOUT)
            if r.status_code in (200, 404):
                log(f"Tijdelijke {what} opgeruimd")
            else:
                warn(f"Opruimen {what} mislukt: HTTP {r.status_code} - {r.text}")
        except requests.RequestException as e:
            warn(f"Opruimen {what} mislukt: {e} - ruim handmatig op!")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default="config.ini", help="pad naar config.ini")
    ap.add_argument("--policy-id", help="overschrijft policy_id uit config.ini")
    ap.add_argument("--force", action="store_true",
                    help="policy ook verwijderen als er nog agents aan hangen")
    ap.add_argument("--dry-run", action="store_true",
                    help="alleen login en lookup, niets wijzigen")
    ap.add_argument("-y", "--yes", action="store_true", help="niet om bevestiging vragen")
    args = ap.parse_args()

    cfg = load_config(args.config)
    policy_id = args.policy_id or cfg["policy_id"]
    if not policy_id:
        sys.exit("Geen policy-id opgegeven (config.ini policy_id of --policy-id)")

    if cfg["verify"] is False:
        requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]

    password = getpass.getpass(f"Wachtwoord voor '{cfg['username']}': ")
    admin = make_session(cfg["username"], password, cfg["verify"])

    try:
        verify_login(admin, cfg)
    except (RuntimeError, requests.RequestException) as e:
        warn(str(e))
        return 1

    if args.dry_run:
        log("Dry-run: deletion records opzoeken met eigen account (kan falen zonder rechten)")
        try:
            print(find_records(admin, cfg, policy_id) or "geen records gevonden")
        except (RuntimeError, requests.RequestException) as e:
            warn(str(e))
        return 0

    if not args.yes:
        ans = input(f"Policy '{policy_id}' en het deletion record verwijderen? [j/N] ")
        if ans.strip().lower() not in ("j", "ja", "y", "yes"):
            print("Afgebroken.")
            return 0

    temp_pw = gen_password()
    rc = 0
    try:
        create_temp_role_user(admin, cfg, temp_pw)
        delete_policy(admin, cfg, policy_id, args.force)

        temp = make_session(TEMP_USER, temp_pw, cfg["verify"])
        ids = find_records(temp, cfg, policy_id)
        if not ids:
            warn(f"Geen {RECORD_TYPE} gevonden voor '{policy_id}'")
        else:
            log(f"Gevonden record(s): {', '.join(ids)}")
            delete_records(temp, cfg, ids)
            if find_records(temp, cfg, policy_id):
                warn("Record bestaat nog steeds na verwijderen!")
                rc = 1
            else:
                log("Controle OK: geen deletion record meer aanwezig")
    except (RuntimeError, requests.RequestException) as e:
        warn(str(e))
        rc = 1
    finally:
        cleanup(admin, cfg)

    if rc == 0:
        log("Klaar. Herstart Kibana zodat de policy opnieuw wordt aangemaakt, bijv.:")
        print("    oc delete pod -l kibana.k8s.elastic.co/name=<kibana-naam> -n <namespace>")
    return rc


if __name__ == "__main__":
    sys.exit(main())
