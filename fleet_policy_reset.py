#!/usr/bin/env python3
"""
Verwijdert een (gepreconfigureerde) Fleet agent policy én het bijbehorende
fleet-preconfiguration-deletion-record, zodat Kibana de policy bij de
volgende herstart opnieuw aanmaakt vanuit de ECK/Kibana-config.

Stappen:
  1. Inloggen op Elasticsearch en Kibana (wachtwoord wordt gevraagd)
  2. Bestaande policies tonen en vragen welk policy-ID verwijderd moet worden
  3. Gekoppelde agents tonen
  4. Tijdelijke rol + user aanmaken (rechten op .kibana_ingest*)
  5. Gekoppelde agents unenrollen (tenzij --keep-agents)
  6. Agent policy verwijderen via de Fleet API
  7. Deletion record opzoeken en verwijderen (als tijdelijke user)
  8. Tijdelijke rol + user opruimen (altijd, ook bij fouten)

Config: fleet_policy_reset.ini in de huidige map of ~/.config/fleet_policy_reset.ini
(of -c pad). Zie fleet_policy_reset.ini.example.

Gebruik:
  ./fleet_policy_reset.py                      # vraagt om wachtwoord en policy-ID
  ./fleet_policy_reset.py -c andere.ini
  ./fleet_policy_reset.py --policy-id Kafka-consumer --force
  ./fleet_policy_reset.py --dry-run            # alleen tonen, niets wijzigen
"""

import argparse
import configparser
import getpass
import os
import secrets
import string
import sys
import time
from typing import Optional, Tuple
from urllib.parse import quote

import requests

CONFIG_NAME = "fleet_policy_reset.ini"
CONFIG_PATHS = [CONFIG_NAME, os.path.expanduser(f"~/.config/{CONFIG_NAME}")]

TEMP_ROLE = "kibana_ingest_admin"
TEMP_USER = "tmp_ingest_admin"
RECORD_TYPE = "fleet-preconfiguration-deletion-record"
DEFAULT_TIMEOUT = 60

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


def check(resp: requests.Response, what: str, ok=(200, 201)) -> dict:
    if resp.status_code not in ok:
        raise RuntimeError(f"{what} mislukt: HTTP {resp.status_code} - {resp.text}")
    try:
        return resp.json()
    except ValueError:
        return {}


def gen_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits + "-_.!"
    return "".join(secrets.choice(alphabet) for _ in range(length))


class Endpoint:
    """Eén Elasticsearch- of Kibana-endpoint met eigen URL, TLS-instellingen en timeout."""

    def __init__(self, name: str, url: str, username: str, verify, timeout: int):
        self.name = name
        self.url = url.rstrip("/")
        self.username = username
        self.verify = verify
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify
        self.session.headers.update({"Content-Type": "application/json", "kbn-xsrf": "true"})

    def login(self, password: str) -> None:
        self.session.auth = (self.username, password)

    def as_user(self, username: str, password: str) -> "Endpoint":
        other = Endpoint(self.name, self.url, username, self.verify, self.timeout)
        other.login(password)
        return other

    def request(self, method: str, path: str, **kw) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        return self.session.request(method, f"{self.url}{path}", **kw)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def put(self, path, **kw):
        return self.request("PUT", path, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _verify_setting(section: configparser.SectionProxy):
    """ca_cert gaat voor; anders verify_ssl (default true)."""
    ca = section.get("ca_cert", "").strip()
    if ca:
        ca = os.path.expanduser(ca)
        if not os.path.isfile(ca):
            sys.exit(f"[{section.name}] ca_cert niet gevonden: {ca}")
        return ca
    return section.getboolean("verify_ssl", fallback=True)


def find_config(explicit: Optional[str]) -> str:
    if explicit:
        if not os.path.isfile(explicit):
            sys.exit(f"Config-bestand niet gevonden: {explicit}")
        return explicit
    for p in CONFIG_PATHS:
        if os.path.isfile(p):
            return p
    sys.exit(f"Geen config gevonden. Kopieer {CONFIG_NAME}.example naar "
             f"{CONFIG_NAME} of ~/.config/{CONFIG_NAME} en pas aan (of gebruik -c).")


def load_config(path: str) -> Tuple["Endpoint", "Endpoint", str]:
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cp.read(path)
    for sec in ("elasticsearch", "kibana"):
        if sec not in cp:
            sys.exit(f"Sectie [{sec}] ontbreekt in {path}")
    endpoints = []
    for sec, label in (("elasticsearch", "Elasticsearch"), ("kibana", "Kibana")):
        s = cp[sec]
        try:
            url, user = s["url"].strip(), s["username"].strip()
        except KeyError as e:
            sys.exit(f"Verplichte instelling {e} ontbreekt in [{sec}] ({path})")
        timeout = s.getint("timeout", fallback=DEFAULT_TIMEOUT)
        endpoints.append(Endpoint(label, url, user, _verify_setting(s), timeout))
    default_policy = cp.get("reset", "policy_id", fallback="").strip()
    return endpoints[0], endpoints[1], default_policy


# --------------------------------------------------------------------------- #
# Stappen
# --------------------------------------------------------------------------- #
def ask_passwords(es: Endpoint, kb: Endpoint) -> None:
    pw = getpass.getpass(f"Wachtwoord voor '{es.username}' (Elasticsearch): ")
    es.login(pw)
    if kb.username == es.username:
        kb.login(pw)
    else:
        kb.login(getpass.getpass(f"Wachtwoord voor '{kb.username}' (Kibana): "))


def verify_login(es: Endpoint, kb: Endpoint) -> None:
    data = check(es.get("/_security/_authenticate"), "Inloggen op Elasticsearch")
    log(f"Ingelogd op Elasticsearch ({es.url}) als '{data.get('username')}'")
    check(kb.get("/api/status"), "Inloggen op Kibana")
    log(f"Ingelogd op Kibana ({kb.url}) als '{kb.username}'")


def list_policies(kb: Endpoint) -> list:
    r = kb.get("/api/fleet/agent_policies", params={"perPage": 1000})
    return check(r, "Agent policies ophalen").get("items", [])


def prompt_policy_id(kb: Endpoint, default: str) -> str:
    """Toont de bestaande policies en vraagt welke verwijderd moet worden."""
    try:
        policies = list_policies(kb)
    except (RuntimeError, requests.RequestException) as e:
        warn(f"Kon policies niet ophalen: {e}")
        policies = []

    if policies:
        print("\nBestaande agent policies:")
        w = max(2, *(len(p.get("id", "")) for p in policies))
        print(f"  {'ID'.ljust(w)}  {'MANAGED':7}  NAAM")
        for p in sorted(policies, key=lambda x: x.get("id", "")):
            print(f"  {p.get('id', '').ljust(w)}  "
                  f"{str(p.get('is_managed', False)):7}  {p.get('name', '')}")
        print("  (een al verwijderde policy staat hier niet meer; je kunt het ID toch invullen)\n")

    hint = f" [{default}]" if default else ""
    ans = input(f"Policy-ID om te verwijderen{hint}: ").strip()
    return ans or default


def create_temp_role_user(es: Endpoint, temp_pw: str) -> None:
    check(es.put(f"/_security/role/{TEMP_ROLE}", json=ROLE_BODY), f"Rol '{TEMP_ROLE}' aanmaken")
    log(f"Tijdelijke rol '{TEMP_ROLE}' aangemaakt")
    check(es.post(f"/_security/user/{TEMP_USER}",
                  json={"password": temp_pw, "roles": [TEMP_ROLE],
                        "full_name": "Tijdelijke user fleet_policy_reset.py"}),
          f"User '{TEMP_USER}' aanmaken")
    log(f"Tijdelijke user '{TEMP_USER}' aangemaakt")


def list_policy_agents(kb: Endpoint, policy_id: str) -> list:
    """Alle (niet al ge-unenrollde) agents die aan de policy gekoppeld zijn, ook offline/inactief."""
    r = kb.get("/api/fleet/agents",
               params={"kuery": f'policy_id:"{policy_id}"', "perPage": 1000,
                       "showInactive": "true"})
    data = check(r, f"Agents van policy '{policy_id}' ophalen")
    agents = data.get("items", data.get("list", []))
    return [a for a in agents
            if a.get("policy_id") == policy_id and a.get("status") != "unenrolled"]


def agent_hostname(agent: dict) -> str:
    return (agent.get("local_metadata", {}).get("host", {}).get("hostname")
            or agent.get("local_metadata", {}).get("host", {}).get("name") or "?")


def show_agents(agents: list) -> None:
    w = max(8, *(len(agent_hostname(a)) for a in agents))
    print(f"  {'HOSTNAME'.ljust(w)}  {'STATUS':10}  AGENT-ID")
    for a in sorted(agents, key=agent_hostname):
        print(f"  {agent_hostname(a).ljust(w)}  {str(a.get('status', '?')):10}  {a.get('id')}")


def unenroll_agents(kb: Endpoint, policy_id: str, agents: list, wait: int = 60) -> None:
    """Unenrollt de agents direct (revoke) en wacht tot de policy geen agents meer heeft."""
    ids = [a["id"] for a in agents]
    check(kb.post("/api/fleet/agents/bulk_unenroll", json={"agents": ids, "revoke": True}),
          "Agents unenrollen")
    log(f"Unenroll gestart voor {len(ids)} agent(s), wachten tot ze van de policy af zijn...")
    deadline = time.time() + wait
    while True:
        remaining = list_policy_agents(kb, policy_id)
        if not remaining:
            log("Alle agents zijn ge-unenrolled")
            return
        if time.time() >= deadline:
            raise RuntimeError(
                f"Na {wait}s nog {len(remaining)} agent(s) gekoppeld: "
                + ", ".join(agent_hostname(a) for a in remaining))
        time.sleep(3)


def delete_policy(kb: Endpoint, policy_id: str, force: bool) -> None:
    r = kb.get(f"/api/fleet/agent_policies/{quote(policy_id, safe='')}")
    if r.status_code == 404:
        warn(f"Policy '{policy_id}' bestaat niet (meer) - ga door met opruimen record")
        return
    data = check(r, f"Policy '{policy_id}' ophalen").get("item", {})
    log(f"Policy gevonden: '{data.get('name')}' (managed: {data.get('is_managed')}, "
        f"preconfigured: {data.get('is_preconfigured')})")

    remaining = list_policy_agents(kb, policy_id)
    if remaining and not force:
        raise RuntimeError(
            f"Er zijn nog {len(remaining)} agent(s) aan deze policy gekoppeld. "
            "Laat het script ze unenrollen (zonder --keep-agents), of gebruik --force.")

    body = {"agentPolicyId": policy_id}
    if force:
        body["force"] = True
    check(kb.post("/api/fleet/agent_policies/delete", json=body),
          f"Policy '{policy_id}' verwijderen")
    log(f"Policy '{policy_id}' verwijderd")


def find_records(es: Endpoint, policy_id: str, retries: int = 5) -> list:
    query = {"size": 100, "query": {"term": {"type": RECORD_TYPE}}}
    # Record kan een moment nodig hebben voordat het doorzoekbaar is
    for attempt in range(retries):
        r = es.post("/.kibana_ingest/_search",
                    params={"filter_path": "hits.hits._id,hits.hits._source"}, json=query)
        hits = check(r, "Deletion records opzoeken").get("hits", {}).get("hits", [])
        matches = [h["_id"] for h in hits
                   if h.get("_source", {}).get(RECORD_TYPE, {}).get("id") == policy_id]
        if matches:
            return matches
        if attempt < retries - 1:
            time.sleep(2)
    return []


def delete_records(es: Endpoint, ids: list) -> None:
    for doc_id in ids:
        r = es.delete(f"/.kibana_ingest/_doc/{quote(doc_id, safe='')}", params={"refresh": "true"})
        data = check(r, f"Record '{doc_id}' verwijderen", ok=(200, 404))
        log(f"Record '{doc_id}': {data.get('result', r.status_code)}")


def cleanup(es: Endpoint) -> None:
    for what, path in ((f"user '{TEMP_USER}'", f"/_security/user/{TEMP_USER}"),
                       (f"rol '{TEMP_ROLE}'", f"/_security/role/{TEMP_ROLE}")):
        try:
            r = es.delete(path)
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
    ap.add_argument("-c", "--config",
                    help=f"pad naar config (default: ./{CONFIG_NAME} of ~/.config/{CONFIG_NAME})")
    ap.add_argument("--policy-id", help="policy-ID opgeven i.p.v. erom te vragen")
    ap.add_argument("--keep-agents", action="store_true",
                    help="gekoppelde agents NIET unenrollen (dan stopt het script als er agents zijn)")
    ap.add_argument("--force", action="store_true",
                    help="policy ook verwijderen als er nog agents aan hangen (Fleet 'force')")
    ap.add_argument("--unenroll-timeout", type=int, default=60,
                    help="seconden wachten tot agents ge-unenrolled zijn (default 60)")
    ap.add_argument("--dry-run", action="store_true",
                    help="alleen login en lookup, niets wijzigen")
    ap.add_argument("-y", "--yes", action="store_true", help="niet om bevestiging vragen")
    args = ap.parse_args()

    cfg_path = find_config(args.config)
    es, kb, default_policy = load_config(cfg_path)
    log(f"Config: {cfg_path}")

    if es.verify is False or kb.verify is False:
        requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
        warn("verify_ssl = false: certificaten worden niet gecontroleerd")

    ask_passwords(es, kb)
    try:
        verify_login(es, kb)
    except (RuntimeError, requests.RequestException) as e:
        warn(str(e))
        return 1

    policy_id = args.policy_id or prompt_policy_id(kb, default_policy)
    if not policy_id:
        sys.exit("Geen policy-id opgegeven.")

    try:
        agents = list_policy_agents(kb, policy_id)
    except (RuntimeError, requests.RequestException) as e:
        warn(str(e))
        return 1
    if agents:
        print(f"\nAgents gekoppeld aan '{policy_id}':")
        show_agents(agents)
        print()
        if args.keep_agents:
            warn("--keep-agents: deze agents worden niet ge-unenrolled")
    else:
        log(f"Geen agents gekoppeld aan '{policy_id}'")
    unenroll = bool(agents) and not args.keep_agents

    if args.dry_run:
        log("Dry-run: deletion records opzoeken met eigen account (kan falen zonder rechten)")
        try:
            print(find_records(es, policy_id, retries=1) or "geen records gevonden")
        except (RuntimeError, requests.RequestException) as e:
            warn(str(e))
        return 0

    if not args.yes:
        what = f"{len(agents)} agent(s) unenrollen, " if unenroll else ""
        ans = input(f"{what}policy '{policy_id}' en het deletion record verwijderen? [j/N] ")
        if ans.strip().lower() not in ("j", "ja", "y", "yes"):
            print("Afgebroken.")
            return 0

    temp_pw = gen_password()
    rc = 0
    try:
        create_temp_role_user(es, temp_pw)
        if unenroll:
            unenroll_agents(kb, policy_id, agents, args.unenroll_timeout)
        delete_policy(kb, policy_id, args.force)

        temp = es.as_user(TEMP_USER, temp_pw)
        ids = find_records(temp, policy_id)
        if not ids:
            warn(f"Geen {RECORD_TYPE} gevonden voor '{policy_id}'")
        else:
            log(f"Gevonden record(s): {', '.join(ids)}")
            delete_records(temp, ids)
            if find_records(temp, policy_id, retries=1):
                warn("Record bestaat nog steeds na verwijderen!")
                rc = 1
            else:
                log("Controle OK: geen deletion record meer aanwezig")
    except (RuntimeError, requests.RequestException) as e:
        warn(str(e))
        rc = 1
    finally:
        cleanup(es)

    if rc == 0:
        log("Klaar. Herstart Kibana zodat de policy opnieuw wordt aangemaakt, bijv.:")
        print("    oc delete pod -l kibana.k8s.elastic.co/name=<kibana-naam> -n <namespace>")
        if unenroll:
            log("Herstart daarna ook de ge-unenrollde agents zodat ze opnieuw enrollen, bijv. (ECK):")
            print("    oc delete pod -l agent.k8s.elastic.co/name=<agent-naam> -n <namespace>")
    return rc


if __name__ == "__main__":
    sys.exit(main())
