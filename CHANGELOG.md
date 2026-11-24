# Release notes

## v1.1.0 — 2026-11-24

### Nieuw

- **Gekoppelde agents unenrollen vóór het verwijderen van de policy.** Een policy kan alleen worden verwijderd als er geen agents meer aan hangen. Het script toont nu de gekoppelde agents (hostname, status, agent-ID, ook offline/inactief), unenrollt ze na bevestiging met `revoke` en wacht tot de policy leeg is. Pas daarna wordt de policy verwijderd.
- Optie `--keep-agents`: agents niet unenrollen; het script stopt dan als er nog agents gekoppeld zijn (oude gedrag).
- Optie `--unenroll-timeout`: hoe lang gewacht wordt tot de agents ge-unenrolled zijn (default 60 seconden).
- Aan het eind een hint om ook de ge-unenrollde agents te herstarten, zodat ze opnieuw enrollen.

### Gewijzigd

- Het aantal gekoppelde agents wordt nu via de Agents API bepaald in plaats van via het `agents`-veld van de policy, dat niet in elke versie wordt teruggegeven.
- `--dry-run` toont nu ook welke agents ge-unenrolled zouden worden.
- Met `-y` worden gekoppelde agents zonder bevestiging ge-unenrolled. Gebruik `--keep-agents` als je dat niet wilt.

## v1.0.2 — 2026-11-24

### Opgelost

- Ophalen van de agent policies gaf HTTP 400 door de parameter `withAgentCount`. Die wordt niet meer meegestuurd; de policylijst toont daarom geen aantal agents meer. De controle op gekoppelde agents vóór het verwijderen blijft werken.

## v1.0.1 — 2026-11-24

### Opgelost

- Script startte niet op Python 3.9 en ouder (`TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`). De type hints gebruiken nu `typing.Optional`/`Tuple`; het script werkt weer vanaf Python 3.6.

## v1.0.0 — 2026-11-24

Eerste release van `fleet_policy_reset.py`: een script dat een gepreconfigureerde Fleet agent policy uit ECK reset, zodat Kibana hem bij de volgende herstart opnieuw aanmaakt met de actuele configuratie.

### Achtergrond

Wijzigingen aan policies in `xpack.fleet.agentPolicies` worden niet altijd doorgevoerd in Kibana. Verwijder je de policy, dan legt Kibana een `fleet-preconfiguration-deletion-record` aan in de system index `.kibana_ingest`, waardoor de policy niet opnieuw wordt aangemaakt. Dit script automatiseert de handmatige workaround.

### Functionaliteit

- **Interactief policy kiezen**: toont alle agent policies (ID, managed, naam) en vraagt welk policy-ID verwijderd moet worden. Een al verwijderde policy kan ook worden ingevuld; dan wordt alleen het record opgeruimd.
- **Tijdelijke rechten**: maakt de rol `kibana_ingest_admin` (met `allow_restricted_indices`) en user `tmp_ingest_admin` aan met een willekeurig wachtwoord dat niet wordt getoond.
- **Policy verwijderen** via de Fleet API, met een controle op gekoppelde agents (overslaan met `--force`).
- **Deletion record opzoeken en verwijderen**: alleen records waarvan `id` exact gelijk is aan het policy-ID, gevolgd door een controle dat het record weg is.
- **Altijd opruimen**: de tijdelijke rol en user worden verwijderd, ook als een eerdere stap mislukt.
- **Dry-run** (`--dry-run`): alleen inloggen en opzoeken, niets wijzigen.
- **Bevestiging** voor het verwijderen (overslaan met `-y`).

### Configuratie

- Config-bestand `fleet_policy_reset.ini` in de huidige map of `~/.config/`, of via `-c`. Voorbeeld: `fleet_policy_reset.ini.example`.
- Aparte secties `[elasticsearch]` en `[kibana]` met `url`, `username`, `ca_cert`, `verify_ssl` en `timeout`.
- Geen wachtwoorden in de config: het wachtwoord wordt bij het starten gevraagd, maar één keer als Elasticsearch en Kibana dezelfde username hebben.
- Optioneel `[reset] policy_id` als standaardwaarde bij de vraag om het policy-ID.

### Commandline-opties

| Optie | Omschrijving |
|---|---|
| `-c`, `--config` | Pad naar config-bestand |
| `--policy-id` | Policy-ID direct opgeven, zonder erom te vragen |
| `--force` | Policy ook verwijderen als er nog agents aan gekoppeld zijn |
| `--dry-run` | Alleen login en lookup, niets wijzigen |
| `-y`, `--yes` | Niet om bevestiging vragen |

### Vereisten

- Python 3.6+ en `requests` (`pip install -r requirements.txt`)
- Een Elasticsearch-user die rollen en users mag beheren (bijv. `elastic`) en een Kibana-user die Fleet policies mag verwijderen

### Bekende beperkingen

- Kibana wordt niet automatisch herstart; doe dat zelf na afloop (`oc delete pod -l kibana.k8s.elastic.co/name=<kibana-naam> -n <namespace>`).
- Het script schrijft in een system index. Elasticsearch geeft daarvoor een deprecation-waarschuwing; in een toekomstige major versie kan directe toegang standaard geblokkeerd worden.
- Lukt het opruimen van de tijdelijke user/rol niet (bijv. door een netwerkfout), dan moet je ze handmatig verwijderen.

### Upgrade vanaf een ontwikkelversie

Gebruikte je een eerdere versie uit `main` met `config.ini` (sectie `[elastic]`)? Die wordt niet meer gelezen. Kopieer `fleet_policy_reset.ini.example` naar `fleet_policy_reset.ini` en zet de waarden over.
