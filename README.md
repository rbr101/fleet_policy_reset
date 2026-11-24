# fleet_policy_reset

Script om een gepreconfigureerde Fleet agent policy (gedefinieerd in de Kibana-config van ECK) te resetten, zodat Kibana hem opnieuw aanmaakt met de actuele configuratie.

## Achtergrond

Wijzigingen aan een policy in `xpack.fleet.agentPolicies` (Kibana CR in ECK) worden niet altijd doorgevoerd in Kibana. Verwijder je de policy om hem opnieuw te laten aanmaken, dan legt Kibana een saved object van het type `fleet-preconfiguration-deletion-record` aan in `.kibana_ingest`. Zolang dat record bestaat, maakt Kibana de policy **niet** opnieuw aan.

Dit record staat in een system index en kan zelfs door `elastic` niet zomaar worden verwijderd. Daarvoor is een rol met `allow_restricted_indices: true` nodig.

## Wat het script doet

1. Inloggen op Elasticsearch en Kibana met de gebruiker uit `config.ini` (wachtwoord wordt gevraagd)
2. Tijdelijke rol `kibana_ingest_admin` en user `tmp_ingest_admin` aanmaken (willekeurig wachtwoord, wordt niet getoond)
3. De agent policy verwijderen via de Fleet API
4. Het bijbehorende `fleet-preconfiguration-deletion-record` opzoeken (alleen records waarvan `id` exact gelijk is aan de policy-ID)
5. Het record verwijderen als tijdelijke user en controleren dat het weg is
6. Tijdelijke rol en user opruimen (ook als een eerdere stap mislukt)

Daarna herstart je Kibana zelf, zodat de policy opnieuw wordt aangemaakt.

De tijdelijke rol:

```json
PUT _security/role/kibana_ingest_admin
{ "indices": [ { "names": [".kibana_ingest*"], "privileges": ["read", "write", "delete", "view_index_metadata"], "allow_restricted_indices": true } ] }
```

## Installatie

```bash
git clone https://github.com/rbr101/fleet_policy_reset.git
cd fleet_policy_reset
pip install -r requirements.txt
cp config.ini.example config.ini   # en aanpassen
```

## Configuratie

`config.ini`:

```ini
[elastic]
elasticsearch_url = https://localhost:9200
kibana_url = https://localhost:5601
username = elastic
# true | false | pad naar CA-bestand
verify_ssl = false
policy_id = Kafka-consumer
```

- `username` heeft rechten nodig om rollen en users te beheren en Fleet policies te verwijderen (bijv. `elastic`).
- `policy_id` is hoofdlettergevoelig en moet overeenkomen met de `id` in de Kibana-config.
- `config.ini` staat in `.gitignore`; commit alleen `config.ini.example`.

Op OpenShift/ECK bijvoorbeeld via port-forward:

```bash
oc port-forward svc/<es-naam>-es-http 9200 -n <namespace> &
oc port-forward svc/<kibana-naam>-kb-http 5601 -n <namespace> &
oc get secret <es-naam>-es-elastic-user -n <namespace> -o jsonpath='{.data.elastic}' | base64 -d
```

## Gebruik

```bash
# Eerst testen: alleen inloggen en record opzoeken, niets wijzigen
python3 fleet_policy_reset.py --dry-run

# Uitvoeren (vraagt om bevestiging)
python3 fleet_policy_reset.py

# Andere config / policy, zonder bevestiging, ook als er nog agents aan hangen
python3 fleet_policy_reset.py -c prod.ini --policy-id Kafka-consumer --force -y
```

| Optie | Omschrijving |
|---|---|
| `-c`, `--config` | Pad naar config-bestand (standaard `config.ini`) |
| `--policy-id` | Overschrijft `policy_id` uit de config |
| `--force` | Policy ook verwijderen als er nog agents aan gekoppeld zijn |
| `--dry-run` | Alleen login en lookup, niets wijzigen |
| `-y`, `--yes` | Niet om bevestiging vragen |

Daarna Kibana herstarten en controleren:

```bash
oc delete pod -l kibana.k8s.elastic.co/name=<kibana-naam> -n <namespace>
```

```
GET kbn:/api/fleet/agent_policies/Kafka-consumer
```

## Tips

- Zet `is_managed: false` bij de policy in de Kibana-config als je hem ook in de UI wilt kunnen wijzigen.
- Is de policy al verwijderd, dan slaat het script die stap over en ruimt alleen het record op.
- Mislukt het opruimen van de tijdelijke user/rol (bijv. door netwerkproblemen), verwijder ze dan handmatig:
  ```
  DELETE _security/user/tmp_ingest_admin
  DELETE _security/role/kibana_ingest_admin
  ```
- Het script schrijft in een system index. Maak bij voorkeur eerst een snapshot.

## Handmatige variant

```
POST kbn:/api/fleet/agent_policies/delete
{ "agentPolicyId": "Kafka-consumer" }

GET .kibana_ingest/_search
{ "query": { "term": { "type": "fleet-preconfiguration-deletion-record" } } }

# als user met de rol kibana_ingest_admin:
DELETE .kibana_ingest/_doc/fleet-preconfiguration-deletion-record:<uuid>
```
