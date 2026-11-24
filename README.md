# fleet_policy_reset

Script om een gepreconfigureerde Fleet agent policy (gedefinieerd in de Kibana-config van ECK) te resetten, zodat Kibana hem opnieuw aanmaakt met de actuele configuratie.

Zie [CHANGELOG.md](CHANGELOG.md) voor de release notes.

## Achtergrond

Wijzigingen aan een policy in `xpack.fleet.agentPolicies` (Kibana CR in ECK) worden niet altijd doorgevoerd in Kibana. Verwijder je de policy om hem opnieuw te laten aanmaken, dan legt Kibana een saved object van het type `fleet-preconfiguration-deletion-record` aan in `.kibana_ingest`. Zolang dat record bestaat, maakt Kibana de policy **niet** opnieuw aan.

Dit record staat in een system index en kan zelfs door `elastic` niet zomaar worden verwijderd. Daarvoor is een rol met `allow_restricted_indices: true` nodig.

## Wat het script doet

1. Inloggen op Elasticsearch en Kibana met de gebruikers uit `fleet_policy_reset.ini` (wachtwoord wordt gevraagd; bij dezelfde username maar één keer)
2. De bestaande agent policies tonen en vragen welk policy-ID verwijderd moet worden
3. Tijdelijke rol `kibana_ingest_admin` en user `tmp_ingest_admin` aanmaken (willekeurig wachtwoord, wordt niet getoond)
4. De agent policy verwijderen via de Fleet API
5. Het bijbehorende `fleet-preconfiguration-deletion-record` opzoeken (alleen records waarvan `id` exact gelijk is aan de policy-ID)
6. Het record verwijderen als tijdelijke user en controleren dat het weg is
7. Tijdelijke rol en user opruimen (ook als een eerdere stap mislukt)

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
cp fleet_policy_reset.ini.example fleet_policy_reset.ini   # en aanpassen
```

## Configuratie

Het script zoekt de config in deze volgorde:

1. `-c pad/naar.ini`
2. `./fleet_policy_reset.ini`
3. `~/.config/fleet_policy_reset.ini`

`fleet_policy_reset.ini`:

```ini
[elasticsearch]
url      = https://elasticsearch.example.local:9200
username = elastic
# ca_cert    = /pad/naar/ca.crt   # eigen CA (bijv. ECK: secret <naam>-es-http-certs-public)
# verify_ssl = true               # alleen voor testen op false zetten
# timeout    = 60                 # seconden per call

[kibana]
url      = https://kibana.example.local:5601   # inclusief eventueel basePath
username = elastic
# ca_cert    = /pad/naar/ca.crt
# verify_ssl = true
# timeout    = 60

[reset]
# policy_id = Kafka-consumer
```

| Sectie | Instelling | Omschrijving |
|---|---|---|
| `[elasticsearch]`, `[kibana]` | `url` | Endpoint; voor Kibana inclusief eventueel basePath |
| | `username` | Gebruiker; wachtwoord wordt gevraagd (bij dezelfde username één keer) |
| | `ca_cert` | Pad naar eigen CA; gaat voor op `verify_ssl` |
| | `verify_ssl` | `true` (default) of `false` (alleen voor testen) |
| | `timeout` | Seconden per call (default 60) |
| `[reset]` | `policy_id` | Optioneel: standaardwaarde bij de vraag om het policy-ID (Enter = overnemen) |

- Zet **geen wachtwoord** in de config.
- De Elasticsearch-user heeft rechten nodig om rollen en users te beheren (bijv. `elastic`); de Kibana-user moet Fleet policies kunnen verwijderen.
- `policy_id` is hoofdlettergevoelig en moet overeenkomen met de `id` in `xpack.fleet.agentPolicies`.
- `fleet_policy_reset.ini` staat in `.gitignore`; commit alleen `fleet_policy_reset.ini.example`.
- ECK-CA ophalen: `oc get secret <es-naam>-es-http-certs-public -n <namespace> -o jsonpath='{.data.ca\.crt}' | base64 -d > ca.crt`

Op OpenShift/ECK bijvoorbeeld via port-forward:

```bash
oc port-forward svc/<es-naam>-es-http 9200 -n <namespace> &
oc port-forward svc/<kibana-naam>-kb-http 5601 -n <namespace> &
oc get secret <es-naam>-es-elastic-user -n <namespace> -o jsonpath='{.data.elastic}' | base64 -d
```

## Gebruik

Voorbeeld van de prompt:

```
[+] Config: fleet_policy_reset.ini
Wachtwoord voor 'elastic' (Elasticsearch):
[+] Ingelogd op Elasticsearch (https://localhost:9200) als 'elastic'
[+] Ingelogd op Kibana (https://localhost:5601) als 'elastic'

Bestaande agent policies:
  ID                MANAGED  NAAM
  eck-fleet-server  True     Fleet Server on ECK policy
  Kafka-consumer    False    Kafka consumer policy
  (een al verwijderde policy staat hier niet meer; je kunt het ID toch invullen)

Policy-ID om te verwijderen [Kafka-consumer]:
```

```bash
# Eerst testen: alleen inloggen en record opzoeken, niets wijzigen
./fleet_policy_reset.py --dry-run

# Uitvoeren: vraagt om wachtwoord, toont de policies, vraagt om het policy-ID en om bevestiging
./fleet_policy_reset.py

# Andere config / policy, zonder bevestiging, ook als er nog agents aan hangen
./fleet_policy_reset.py -c prod.ini --policy-id Kafka-consumer --force -y
```

| Optie | Omschrijving |
|---|---|
| `-c`, `--config` | Pad naar config-bestand (standaard `./fleet_policy_reset.ini` of `~/.config/fleet_policy_reset.ini`) |
| `--policy-id` | Policy-ID direct opgeven, zonder erom te vragen |
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
