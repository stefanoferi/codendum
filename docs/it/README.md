# Codendum

[English](../../README.md) · **Italiano**

<!-- translation-source: README.md sha256=81d7f292773ab83455ec0df4077c60d3eb9445ba92e58c89591ba5ae3ecff7de -->

> Questa è la traduzione italiana di [README.md](../../README.md). In caso di
> differenze prevale la versione inglese; vedete
> [docs/TRANSLATING.md](../TRANSLATING.md).

Codendum è un'infrastruttura locale condivisa per agenti di coding. Un solo
sistema NVIDIA GB10 (DGX Spark) esegue [vLLM](https://docs.vllm.ai/) con un
modello per la programmazione, e circa 40 utenti vi si collegano con
[OpenCode](https://opencode.ai/) dalle proprie postazioni. Funziona allo stesso
modo per un'aula di formazione o per un team aziendale: prompt e codice
sorgente restano nella rete dell'organizzazione.

Il repository contiene gli script, le configurazioni d'esempio e la
documentazione per installare, verificare, misurare e gestire il servizio.

> **Stato del progetto: 0.1.0, non ancora rilasciato.** Ogni script e file di
> configurazione è verificato offline in CI (vedete [Sviluppo](#sviluppo)). Il
> servizio è stato anche messo in funzione su un GB10 con 40 utenti OpenCode
> simulati (vedete [Misure su un GB10](#misure-su-un-gb10)). Considerate quei
> valori un riferimento per questa versione di modello e client, non
> prestazioni promesse.

## Indice

- [Come funziona](#come-funziona)
- [Dimensionamento: memoria, contesto e concorrenza](#dimensionamento-memoria-contesto-e-concorrenza)
- [1. Prerequisiti](#1-prerequisiti)
- [2. Configurazione](#2-configurazione)
- [3. Avvio di vLLM](#3-avvio-di-vllm)
- [4. Proxy HTTPS](#4-proxy-https)
- [5. OpenCode sulle postazioni](#5-opencode-sulle-postazioni)
- [6. Smoke test](#6-smoke-test)
- [7. Benchmark](#7-benchmark)
- [8. Profili](#8-profili)
- [9. Risoluzione dei problemi](#9-risoluzione-dei-problemi)
- [10. Limiti e responsabilità](#10-limiti-e-responsabilità)
- [Modello di sicurezza](#modello-di-sicurezza)
- [Componenti, versioni e licenze](#componenti-versioni-e-licenze)
- [Versioni e compatibilità](#versioni-e-compatibilità)
- [Sviluppo](#sviluppo)
- [Riferimenti](#riferimenti)

<!-- section: overview -->
## Come funziona

```text
 Postazioni (x40)                        Host GB10 (DGX OS, ARM64)
 ┌──────────────────────────┐            ┌───────────────────────────────────────────┐
 │ OpenCode                 │  HTTPS     │ container nginx :8443                     │
 │ Git, JDK, Maven/Gradle,  │───────────▶│  reti ammesse, chiave API per utente,     │
 │ build e test             │  /v1/...   │  limiti per utente, solo 2 endpoint       │
 └──────────────────────────┘  LAN/VPN   │        │                                  │
                                         │        ▼ 127.0.0.1:8000                   │
                                         │ container vLLM (modello servito "coder")  │
                                         │ Qwen3-Coder-30B-A3B-Instruct-FP8          │
                                         └───────────────────────────────────────────┘
```

- **Il GB10 serve solo l'inferenza.** Il codice degli utenti, Git, il JDK e le
  build Maven/Gradle girano sulle postazioni o in ambienti di sviluppo isolati
  dedicati. Non lanciate 40 build Java sulla macchina del modello: CPU e GPU
  condividono la stessa memoria.
- **Ogni servizio gira in Docker.** vLLM ascolta solo sull'interfaccia di
  loopback, e un container nginx è l'unico ingresso. nginx accetta HTTPS su una
  porta dedicata dalla LAN o dalla VPN dell'organizzazione, verifica una chiave
  API per utente e inoltra solo `/v1/chat/completions` e `/v1/models`. Gli
  script del repository girano sull'host: pilotano Docker e fanno da client di
  prova, e non installano nulla nel sistema.
- **OpenCode usa un provider OpenAI-compatible esplicito.** Il modello servito
  si chiama `coder`, il tool calling è abilitato esplicitamente e il limite di
  contesto coincide con il profilo del server.

| Percorso | Scopo |
| --- | --- |
| [`scripts/preflight.sh`](../../scripts/preflight.sh) | Controlli dell'host in sola lettura (ARM64, Docker, runtime GPU, memoria, disco, porte) |
| [`scripts/start-vllm.sh`](../../scripts/start-vllm.sh) | Avvia il container vLLM con il profilo scelto |
| [`scripts/start-proxy.sh`](../../scripts/start-proxy.sh) | Avvia, verifica e ricarica il container del proxy nginx |
| [`scripts/smoke-test.sh`](../../scripts/smoke-test.sh) | Controlli di salute, elenco modelli, chat, streaming e chiamata tool |
| [`scripts/metrics.sh`](../../scripts/metrics.sh) | KV cache, richieste in esecuzione/in attesa, preemption, memoria host, GPU |
| [`scripts/bench.sh`](../../scripts/bench.sh) | Prove di carico ripetibili con 1, 8, 16, 24 e 40 richieste simultanee |
| [`scripts/bench-classroom.sh`](../../scripts/bench-classroom.sh) | Classe simulata: 40 utenti con sessioni agentiche in stile OpenCode |
| [`scripts/gen-api-keys.sh`](../../scripts/gen-api-keys.sh) | Genera in locale le chiavi API per utente per nginx |
| [`config/profiles/`](../../config/profiles/) | Profili di servizio (`classroom-64k`, `classroom-64k-high-concurrency`, `deep-128k`) |
| [`config/nginx.example.conf`](../../config/nginx.example.conf) | Reverse proxy HTTPS |
| [`config/opencode.example.json`](../../config/opencode.example.json) | Configurazione del provider per OpenCode V2 |
| [`.env.example`](../../.env.example) | Tutte le impostazioni del server, nessun segreto |

<!-- section: sizing -->
## Dimensionamento: memoria, contesto e concorrenza

**Memoria.** Il GB10 ha **128 GB di memoria unificata condivisa tra CPU e
GPU**. Non sono 128 GB di VRAM che si aggiungono alla RAM di sistema: sistema
operativo, Docker, nginx, i processi di vLLM, i pesi del modello e la KV cache
usano tutti la stessa memoria.

**Pesi.** Il checkpoint FP8 occupa circa 31,2 GB (29,05 GiB) su disco e più o
meno lo stesso in memoria.

**KV cache per token.** Il modello ha 48 layer, 4 teste KV e una dimensione di
testa pari a 128. Con la KV cache in FP8 (1 byte per valore), ogni token tenuto
in contesto costa:

```text
2 (K e V) × 48 layer × 4 teste KV × 128 × 1 byte = 49.152 byte = 48 KiB per token
```

| Contesto attivo | KV cache (teorica) |
| --- | ---: |
| 1 richiesta × 65.536 token | 3 GiB |
| 16 richieste × 65.536 token | 48 GiB |
| 24 richieste × 65.536 token | 72 GiB |
| 40 richieste × 65.536 token | 120 GiB |
| 8 richieste × 131.072 token | 48 GiB |

È un minimo teorico, prima dei pesi e degli altri consumi. Allocazione a
blocchi, condivisione nella prefix cache, CUDA graph, attivazioni, riserve e
lunghezza reale delle richieste modificano l'uso misurato. Un esempio
approssimativo: se CUDA vede circa 120 GiB, `--gpu-memory-utilization 0.80`
assegna a vLLM circa 96 GiB. Tolti pesi e altri consumi restano circa 60 GiB per
la KV cache, cioè circa 1,3 milioni di token o una ventina di contesti completi
da 64K.

All'avvio vLLM scrive nei log i valori reali: una riga `KV cache size: … tokens`
e una riga `Maximum concurrency for … tokens per request`. `scripts/metrics.sh`
mostra la stessa capacità. Sul GB10 usato per le [misure](#misure-su-un-gb10)
CUDA vedeva 121,6 GiB, e il profilo predefinito ha ottenuto 62,4 GiB di KV
cache: 1.362.640 token, cioè 20,8 contesti completi da 64K. Verificate sempre i
valori sul vostro host.

**Collegati, attivi e in attesa sono numeri diversi.**

- *Utenti collegati* (40): hanno OpenCode aperto. Per la maggior parte del
  tempo leggono, scrivono o eseguono test, e non inviano nulla.
- *Richieste attive*: sono in elaborazione. `--max-num-seqs` limita quante
  possono essere eseguite insieme (16 nel profilo predefinito).
- *Richieste in attesa*: sono in coda dentro vLLM, in ordine di arrivo. La coda
  non ha limite, a meno di impostare `CODENDUM_MAX_NUM_QUEUED_REQS`.

`--max-num-seqs` è un tetto e non garantisce né throughput né latenza. Quando la
KV cache si esaurisce, vLLM esegue la *preemption* di una richiesta in corso: ne
libera la memoria e la ricalcola più tardi. L'effetto è un picco di latenza e
un aumento delle `preemptions` nelle metriche.

**Il contesto massimo è un tetto, non una prenotazione.** Aumentare
`max-model-len` non alloca memoria per tutti. Una richiesta davvero lunga però
consuma memoria KV e tempo di prefill: un prompt da 60K token senza riuso della
prefix cache richiede da alcuni secondi a decine di secondi prima del primo
token.

**Si lavora per piccoli passi.** Un progetto Java client-server professionale
si sviluppa modulo per modulo, con compilazione, test e revisione umana dopo
ogni passo. Non nasce da un'unica generazione monolitica. Richieste brevi e
mirate sono anche ciò che mantiene reattivo un servizio condiviso.

<!-- section: prerequisites -->
## 1. Prerequisiti

**Host GB10**

- NVIDIA DGX Spark o altro sistema GB10 con DGX OS (basato su Ubuntu 24.04),
  architettura **ARM64/AArch64**.
- Docker e NVIDIA Container Toolkit. DGX OS li include già configurati. **Non
  reinstallate il driver NVIDIA su un DGX Spark già configurato.**
- `python3` (3.8 o successivo), `curl` e `git`, tutti presenti in DGX OS. nginx
  gira in un container e non va installato sull'host.
- Spazio su disco: circa 30 GiB per la cache del modello e 20–30 GiB per
  l'immagine del container. Tenete liberi almeno 60 GiB.
- Un nome DNS per il servizio (segnaposto: `llm.lab.example`) e un certificato
  TLS riconosciuto dalle postazioni. Usate una CA pubblica con validazione
  DNS-01 oppure la CA interna dell'organizzazione.
- Regole firewall che consentano la porta del proxy (TCP 8443 per impostazione
  predefinita) solo dalla LAN dell'aula o dell'ufficio e dalla VPN.

**Postazioni**

- OpenCode V2. Funziona anche OpenCode 1.x con la configurazione d'esempio V1.
- Gli strumenti di sviluppo del corso o del progetto: Git, JDK, Maven o Gradle
  e così via.

Portate il repository sull'host GB10 ed eseguite i controlli in sola lettura:

```bash
git clone https://github.com/stefanoferi/codendum.git
cd codendum
scripts/preflight.sh
```

`preflight.sh` non modifica nulla. Controlla architettura, Docker e runtime
NVIDIA, `nvidia-smi`, memoria, spazio su disco e porta 8000, e stampa un
suggerimento per ogni avviso o errore. Con `--gpu-test` esegue anche
`nvidia-smi` dentro l'immagine vLLM, una volta scaricata.

<!-- section: configuration -->
## 2. Configurazione

Tutte le impostazioni del server stanno in `.env`, ignorato da Git. I profili
forniscono i parametri di servizio. I valori esportati nella shell prevalgono
su `.env`, e `.env` prevale sul profilo.

```bash
cp .env.example .env
sudo install -d -m 0700 -o "$USER" /etc/codendum
sudo install -d -m 0755 -o "$USER" /var/lib/codendum/huggingface
```

Se l'account di servizio non ha i permessi `sudo`, usate directory di sua
proprietà, ad esempio `/home/codendum/codendum-data/huggingface` per
`CODENDUM_HF_CACHE_DIR` e `/home/codendum/codendum-data/proxy` per
`CODENDUM_PROXY_DIR`. I valori in `.env` non vengono espansi: scrivete percorsi
assoluti.

Rivedete `.env`. Le impostazioni principali sono:

| Variabile | Predefinito | Significato |
| --- | --- | --- |
| `CODENDUM_PROFILE` | `classroom-64k` | Profilo di servizio (vedete [Profili](#8-profili)) |
| `CODENDUM_VLLM_IMAGE` | `vllm/vllm-openai:v0.30.0@sha256:8a69…` | Immagine del container, fissata con tag e digest |
| `CODENDUM_MODEL` / `CODENDUM_MODEL_REVISION` | `Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8` @ `dcaee4d4…` | Checkpoint e revisione esatta |
| `CODENDUM_SERVED_MODEL_NAME` | `coder` | Nome del modello usato dai client |
| `CODENDUM_HOST` / `CODENDUM_PORT` | `127.0.0.1` / `8000` | Indirizzo dell'API (solo loopback, imposto dallo script) |
| `CODENDUM_HF_CACHE_DIR` | `/var/lib/codendum/huggingface` | Cache persistente del modello |
| `CODENDUM_SECRETS_ENV_FILE` | vuoto | File opzionale con i segreti per il container |
| `CODENDUM_HF_OFFLINE` | `0` | Impostate `1` dopo il primo download |
| `CODENDUM_TOOL_CALL_PARSER` | `qwen3_coder` | Parser delle chiamate tool (lo stesso di `qwen3_xml` in vLLM ≥ 0.25) |
| `CODENDUM_SSE_KEEPALIVE_SECONDS` | `15` | Commenti keep-alive mentre una richiesta in streaming attende |
| `CODENDUM_MAX_NUM_QUEUED_REQS` | vuoto | Limite opzionale della coda (HTTP 503 oltre il limite) |
| `CODENDUM_PROXY_IMAGE` | `nginx:1.30.5-alpine@sha256:0985…` | Immagine del container del proxy, fissata con tag e digest |
| `CODENDUM_PROXY_DIR` | `/etc/codendum/proxy` | Configurazione, certificato, mappa delle chiavi e log del proxy |

I segreti non vanno mai in `.env`, nel repository o su una riga di comando. Il
modello usato qui non è ad accesso riservato, quindi non serve un token Hugging
Face. Se un giorno servisse (`HF_TOKEN=...`), o se volete la chiave API propria
di vLLM (`VLLM_API_KEY=...`, vedete [la sezione sul proxy](#4-proxy-https)),
mettetelo in un file leggibile solo dall'utente che esegue `start-vllm.sh`:

```bash
install -m 0600 /dev/null /etc/codendum/vllm.secrets.env
```

Poi impostate `CODENDUM_SECRETS_ENV_FILE=/etc/codendum/vllm.secrets.env` in
`.env`. Il file viene passato a Docker con `--env-file` e non viene mai
stampato. Chiunque possa eseguire `docker inspect` può leggerlo, ma l'accesso a
Docker sull'host equivale comunque a quello di root.

<!-- section: launch -->
## 3. Avvio di vLLM

Verificate prima la configurazione e il comando esatto, poi avviate il
container:

```bash
scripts/start-vllm.sh --dry-run
scripts/start-vllm.sh --wait
```

Il primo avvio scarica circa 31 GB di pesi, quindi `--wait` può richiedere un
po' di tempo. `CODENDUM_START_TIMEOUT` (3600 s per impostazione predefinita)
limita sia l'attesa sia il periodo di tolleranza dell'health check di Docker.
Con una connessione lenta aumentatelo prima del primo avvio: a 5 MB/s il solo
download richiede circa 100 minuti. Altrimenti Docker segnala il container come
`unhealthy` mentre sta ancora scaricando.

Lo script:

- valida ogni impostazione e rifiuta immagini non fissate e qualsiasi indirizzo
  diverso dal loopback;
- non sostituisce mai un container esistente senza `--replace`;
- avvia il container con la GPU, la rete dell'host (l'API ascolta solo su
  `127.0.0.1:8000`), una cache persistente del modello, una politica di
  riavvio, log a rotazione e un health check Docker;
- abilita chunked prefill, prefix caching, scelta automatica dei tool con il
  parser `qwen3_coder` e KV cache in FP8.

Leggete dai log la capacità reale della KV cache:

```bash
docker logs codendum-vllm 2>&1 | grep -E "KV cache size|Maximum concurrency"
```

Per la gestione quotidiana si usano i normali comandi Docker:

```bash
docker logs -f codendum-vllm
docker inspect --format '{{.State.Health.Status}}' codendum-vllm
docker restart codendum-vllm
docker stop codendum-vllm
docker start codendum-vllm
```

Dopo il primo download riuscito impostate `CODENDUM_HF_OFFLINE=1`, così i
riavvii non contattano mai l'Hugging Face Hub.

### Aggiornamenti

vLLM esce spesso con nuove versioni, e un flag o un kernel possono cambiare
comportamento. Aggiornate in una finestra di manutenzione:

1. Scegliete la nuova versione e leggetene le note di rilascio.
2. Ricavatene il digest e verificate che l'indice includa `linux/arm64`.
3. Aggiornate `CODENDUM_VLLM_IMAGE` in `.env`, lasciando la riga precedente
   commentata per il ritorno indietro.
4. Ricreate il container, poi eseguite lo smoke test e il benchmark.

```bash
docker buildx imagetools inspect vllm/vllm-openai:v0.30.0
scripts/start-vllm.sh --replace --wait
scripts/smoke-test.sh
```

Per tornare indietro ripristinate la riga dell'immagine precedente ed eseguite
di nuovo `scripts/start-vllm.sh --replace --wait`. La revisione del modello si
fissa allo stesso modo con `CODENDUM_MODEL_REVISION`.

**Scelta dell'immagine.** L'immagine predefinita è quella upstream
`vllm/vllm-openai`, fissata tramite digest. È la stessa che l'attuale playbook
vLLM di NVIDIA per DGX Spark usa per il servizio su un singolo nodo. La
versione 0.30.0 è costruita su CUDA 13.0.2, la versione CUDA del driver di DGX
Spark. L'immagine NGC di NVIDIA (`nvcr.io/nvidia/vllm`, ad esempio `26.08-py3`
con vLLM 0.27.1) è un'alternativa, ma non è stata provata con questo progetto.
Tre cose da sapere se la usate:

- `start-vllm.sh` sostituisce l'entrypoint dell'immagine con `vllm`, quindi lo
  script di entrypoint di NVIDIA non viene eseguito.
- Le versioni di vLLM precedenti alla 0.29 non supportano
  `CODENDUM_MAX_NUM_QUEUED_REQS`: lasciatela vuota.
- Se l'immagine non accetta `--sse-keep-alive-interval`, impostate
  `CODENDUM_SSE_KEEPALIVE_SECONDS=0`.

Per controllare la versione di vLLM dentro un'immagine:

```bash
docker run --rm --entrypoint python3 vllm/vllm-openai:v0.30.0@sha256:8a69ffad015f138d7170c4ddc429e230a3bc1c1719f67e14324749df200a4b90 -c "import vllm; print(vllm.__version__)"
```

<!-- section: proxy -->
## 4. Proxy HTTPS

nginx gira in un container dedicato, `codendum-proxy`, su una porta riservata
(8443 per impostazione predefinita), così non interferisce con altri servizi web
dell'host. Termina il TLS, accetta solo le reti ammesse, associa ogni chiave API
a un utente, applica i limiti per utente e inoltra due endpoint a vLLM. Tutto il
resto, compresi `/health`, `/metrics`, `/tokenize`, `/invocations` e il resto
dell'API di vLLM, resta raggiungibile solo dall'host. Il proxy serve l'API per
OpenCode e per gli altri client compatibili con OpenAI: non ha
un'interfaccia web.

**1. Create la directory del proxy.** `--init` crea `/etc/codendum/proxy`
(`CODENDUM_PROXY_DIR`) con `conf.d/`, `tls/` e `logs/`, e copia la configurazione
d'esempio. Non sovrascrive mai file esistenti:

```bash
scripts/start-proxy.sh --init
```

**2. Adattate la configurazione del sito** in
`/etc/codendum/proxy/conf.d/codendum.conf`:

- il nome host;
- gli intervalli LAN/VPN (`192.0.2.0/24` e `198.51.100.0/24` sono segnaposto
  riservati alla documentazione);
- la porta nella riga `listen`, se la 8443 è già occupata sull'host;
- i limiti, se serve.

**3. Installate certificato e chiave** della vostra CA come `fullchain.pem` e
`privkey.pem`, leggibili solo dall'account di servizio:

```bash
install -m 0600 fullchain.pem privkey.pem /etc/codendum/proxy/tls/
```

**4. Generate in locale una chiave API per utente.** Le chiavi sono valori
casuali a 256 bit con prefisso `cdm_`. Vengono scritte con permessi 0600, non
sovrascrivono mai file esistenti, e lo script si rifiuta di scriverle dentro
una working tree Git a meno che Git non ignori quel percorso:

```bash
KEYDIR="$HOME/codendum-keys/$(date -u +%Y%m%d)"
scripts/gen-api-keys.sh --out-dir "$KEYDIR" --count 40
install -m 0600 "$KEYDIR/api-keys.map" /etc/codendum/proxy/api-keys.map
```

`api-keys.csv`, nella stessa directory, elenca le coppie `user_id,api_key` da
distribuire. Consegnate a ogni utente la sua chiave attraverso un canale
privato, poi cancellate il CSV o conservatelo nel gestore di password
dell'organizzazione. Con `--users-file nomi.txt` potete usare identificativi
vostri.

- Per **ruotare** le chiavi generate una nuova directory, installate la nuova
  mappa e ricaricate il proxy.
- Per **revocare** una singola chiave cancellate la sua riga dalla mappa e
  ricaricate il proxy.

**5. Avviate il proxy.** Lo script controlla i file e i loro permessi e prova
la configurazione in un container temporaneo. Si rifiuta di sostituire un proxy
esistente senza `--replace`:

```bash
scripts/start-proxy.sh --dry-run
scripts/start-proxy.sh
```

Il container è irrobustito:

- rete dell'host, così nginx raggiunge vLLM su `127.0.0.1:8000`;
- file system radice in sola lettura;
- nessuna capability oltre alle poche che servono a nginx per partire;
- `no-new-privileges`.

I log finiscono in `/etc/codendum/proxy/logs/`. Dopo aver cambiato chiavi,
certificati o configurazione, ricaricate senza interruzioni:

```bash
scripts/start-proxy.sh --reload
```

**6. Limitate la porta anche nel firewall.** Se l'host usa `ufw`, le regole
sono simili a queste; prima di abilitare un firewall assicuratevi che SSH
resti consentito:

```bash
sudo ufw allow from 192.0.2.0/24 to any port 8443 proto tcp
sudo ufw allow from 198.51.100.0/24 to any port 8443 proto tcp
```

**Usare invece l'nginx della distribuzione.** La stessa configurazione funziona
con il pacchetto nginx dell'host, che la legge da `/etc/nginx/conf.d/` e si
aspetta certificato e mappa delle chiavi in `/etc/nginx/codendum/`:

```bash
sudo install -d -m 0700 -o root -g root /etc/nginx/codendum /etc/nginx/codendum/tls
sudo install -m 0600 -o root -g root /etc/codendum/proxy/tls/fullchain.pem /etc/codendum/proxy/tls/privkey.pem /etc/nginx/codendum/tls/
sudo install -m 0600 -o root -g root /etc/codendum/proxy/api-keys.map /etc/nginx/codendum/api-keys.map
sudo install -m 0644 -o root -g root /etc/codendum/proxy/conf.d/codendum.conf /etc/nginx/conf.d/codendum.conf
sudo nginx -t
sudo systemctl reload nginx
```

### Risposte del proxy

| Stato | Significato |
| --- | --- |
| 401 | Chiave API mancante o sconosciuta |
| 403 | Indirizzo del client fuori dalle reti ammesse |
| 404 | Endpoint non esposto |
| 429 | Limite per utente superato (3 richieste in corso, 60 al minuto con un burst di 30 richieste) o tetto globale (64 in corso) |
| 502 / 504 | vLLM spento, ancora in avvio, o senza risposta entro il timeout |
| 400 (da vLLM) | Richiesta non valida, ad esempio un prompt più lungo di `max-model-len` |
| 503 (da vLLM) | Limite della coda raggiunto, solo se è impostato `CODENDUM_MAX_NUM_QUEUED_REQS` |

nginx, con la configurazione standard, risponde 503 quando scatta un limite
`limit_conn` o `limit_req`. Questa configurazione usa invece 429, con
un'intestazione `Retry-After`, così "rallenta" si distingue da "server non
disponibile".

### Coda ed equità

- **nginx limita le richieste, non i token.** Il tetto per utente impedisce a
  una persona, o a un agente che lancia sotto-agenti, di occupare tutti i posti.
  Non rende equo l'uso: una richiesta da 60K token costa molto più di una da 2K.
- **vLLM serve in ordine di arrivo.** Esegue fino a `max-num-seqs` richieste e
  mette le altre in coda, senza limite. Con `CODENDUM_MAX_NUM_QUEUED_REQS=N`
  (vLLM ≥ 0.29), le richieste oltre N, tra attive e in attesa, ricevono subito
  un HTTP 503 invece di un'attesa lunga.
- **Il chunked prefill** divide i prompt lunghi in passi da 8.192 token
  (`max-num-batched-tokens`), così l'output degli altri utenti continua a
  scorrere durante un prefill grande. Il prefill grande richiede comunque
  tempo.
- **Quote di token, budget o priorità per utente** richiedono un gateway LLM
  dedicato con autenticazione davanti a vLLM. Codendum non ne include né ne
  verifica uno.

### Identità e autenticazione verso vLLM

- **Identità dell'organizzazione.** Se l'organizzazione dispone di un gateway
  di autenticazione (SSO o proxy basato sull'identità), questo può emettere o
  associare le chiavi per utente al posto di `gen-api-keys.sh`. Valgono due
  vincoli: OpenCode invia una chiave bearer statica, e nginx deve comunque
  ricevere una chiave che sappia associare a un utente.
- **Autenticazione, quota, richieste attive e dimensione della coda sono
  controlli distinti.** nginx fornisce il primo e una forma semplice del terzo;
  vLLM fornisce il quarto.
- **Chiave verso vLLM (opzionale).** L'opzione `--api-key` di vLLM protegge solo
  le route sotto `/v1`, `/v2`, `/inference` e `/cohere`. `/metrics`, `/health`,
  `/tokenize` e `/invocations` restano aperte sull'interfaccia di loopback. La
  vera protezione è quindi il binding su loopback, insieme a un accesso
  limitato alla shell e a Docker sull'host. Per una difesa in profondità:
  1. Impostate `VLLM_API_KEY=...` nel file dei segreti.
  2. Mettete `proxy_set_header Authorization "Bearer ...";` in un file con
     permessi 0600 nella directory del proxy e includetelo al posto della riga
     `proxy_set_header Authorization "";`.

<!-- section: opencode -->
## 5. OpenCode sulle postazioni

Installate OpenCode V2 seguendo le
[istruzioni ufficiali](https://opencode.ai/v2/docs/). Sulle macchine gestite
preferite la distribuzione software dell'organizzazione. Poi configurate il
provider:

1. Copiate [`config/opencode.example.json`](../../config/opencode.example.json)
   in `~/.config/opencode/opencode.json`, oppure in `opencode.json` dentro un
   progetto.
2. Sostituite `https://llm.lab.example:8443/v1` con l'URL del servizio, porta
   del proxy compresa.
3. Fornite la chiave dell'utente nella variabile d'ambiente `CODENDUM_API_KEY`.
   La configurazione la richiama come `{env:CODENDUM_API_KEY}`.

```json
"codendum": {
  "name": "Codendum (local)",
  "package": "@opencode/ai/providers/openai-compatible",
  "settings": { "baseURL": "https://llm.lab.example:8443/v1", "apiKey": "{env:CODENDUM_API_KEY}" },
  "models": {
    "coder": {
      "modelID": "coder",
      "capabilities": { "tools": true, "input": ["text"], "output": ["text"] },
      "limit": { "context": 65536, "output": 8192 }
    }
  }
}
```

Perché conta ogni impostazione:

- **`capabilities.tools: true` è obbligatorio.** La scoperta automatica dei
  modelli vLLM in OpenCode non vede il supporto ai tool lato server, quindi i
  modelli scoperti partono con i tool disabilitati. Dichiarate il modello
  esplicitamente e non chiamate il provider `vllm`: quell'identificativo
  appartiene al plugin di scoperta integrato.
- **`limit.context` deve coincidere con `max-model-len` del server.** OpenCode
  lo usa per gestire la dimensione della conversazione, ad esempio per decidere
  quando compattarla. Se supera il limite del server, le sessioni lunghe
  falliscono con HTTP 400. Per il profilo `deep-128k` portatelo a `131072` su
  tutte le postazioni, insieme al cambio sul server.
- **OpenCode V2 usa un servizio in background,** che vede una variabile
  d'ambiente solo se era impostata quando il servizio è partito. Avviate
  OpenCode da una shell che esporta `CODENDUM_API_KEY`, oppure seguite la
  [documentazione di rete](https://opencode.ai/v2/docs/network) di V2 per
  salvarla nella configurazione del servizio.
- **Certificati emessi da una CA interna** richiedono
  `NODE_EXTRA_CA_CERTS=/percorso/ca.pem` sulla postazione.
- **OpenCode 1.x** usa il formato precedente: vedete
  [`config/opencode.v1.example.json`](../../config/opencode.v1.example.json).
- **La condivisione delle sessioni è disattivata** in entrambi gli esempi
  (`"share": "disabled"`). Le sessioni OpenCode condivise sono link pubblici
  ospitati fuori dall'organizzazione.
- **Gli avvisi dell'editor sulle chiavi V2 sono previsti.** Gli editor che
  validano con `https://opencode.ai/config.json` possono segnalare `providers`,
  `package`, `settings` e `capabilities`, perché lo schema pubblicato descrive
  ancora il formato V1. OpenCode V2 le accetta.

Una risposta in chat da sola non dimostra che l'agente funzioni. Verificate un
ciclo reale lettura → modifica → test in una directory di prova, su una
postazione con JDK:

```bash
mkdir -p /tmp/codendum-check && cd /tmp/codendum-check
cat > Calc.java <<'EOF'
public class Calc {
    static int add(int a, int b) { return a - b; }
    public static void main(String[] args) {
        if (add(2, 3) != 5) throw new AssertionError("add(2, 3) should be 5");
        System.out.println("OK");
    }
}
EOF
opencode run --auto --model codendum/coder "Run 'java Calc.java', fix the bug in Calc.java, then run it again until it prints OK."
java Calc.java
```

La verifica è superata quando OpenCode ha letto il file, lo ha modificato con i
suoi tool e ha eseguito il programma, e l'ultimo `java Calc.java` stampa `OK`.
`--auto` approva automaticamente i permessi dei tool: usatelo solo in una
directory di prova.

<!-- section: smoke-test -->
## 6. Smoke test

Sull'host GB10, verificate direttamente vLLM:

```bash
scripts/smoke-test.sh
```

Poi verificate attraverso il proxy con una chiave utente. In questo caso lo
script controlla anche che `/health`, `/metrics` e gli altri endpoint privati
**non** siano raggiungibili, e che le richieste senza chiave vengano
rifiutate:

```bash
read -rsp "API key: " CODENDUM_API_KEY && export CODENDUM_API_KEY
scripts/smoke-test.sh --proxy --base-url https://llm.lab.example:8443
```

Aggiungete `--cacert /percorso/ca.pem` per un certificato emesso da una CA
privata.

I controlli vengono eseguiti in quest'ordine:

1. Salute del servizio ed elenco dei modelli, che deve contenere `coder`.
2. Un completamento in chat.
3. Un completamento in streaming, che deve arrivare a blocchi e terminare con
   `[DONE]`.
4. Una chiamata tool con `tool_choice: "auto"`. È il percorso usato da
   OpenCode, quello che mette alla prova il parser `qwen3_coder`.
5. Una chiamata tool con `tool_choice: "required"`. Passa dagli output
   strutturati e non verifica il parser.
6. Un giro completo in cui si rimanda il risultato del tool e ci si aspetta una
   risposta testuale.

Il codice di uscita è `0` se tutti i controlli passano e `1` se uno fallisce.
`2` indica un risultato non conclusivo: ad esempio, il modello ha
legittimamente risposto senza chiamare il tool con `"auto"`. Un risultato non
conclusivo non è un successo. Ripetete la prova e usate il ciclo OpenCode
descritto sopra per decidere. Se nel testo del messaggio compare il markup
grezzo `<tool_call>`, il parser è configurato male e il controllo fallisce.

<!-- section: benchmark -->
## 7. Benchmark

> Eseguite i benchmark solo in una finestra di manutenzione. Mettono il server
> sotto carico continuo per molti minuti. Lo script si rifiuta di partire se ci
> sono richieste in esecuzione o in attesa, a meno di `--allow-busy`, e chiede
> conferma, a meno di `--yes`.

```bash
scripts/metrics.sh
scripts/bench.sh
scripts/bench.sh --concurrency 1,8,16 --shapes short --yes
scripts/metrics.sh --watch 5
```

`bench.sh` genera un carico a ciclo chiuso. A ogni livello di concorrenza
(predefiniti 1, 8, 16, 24 e 40) mantiene N richieste in corso, con prompt
brevi (circa 512 token in ingresso) e lunghi (circa 16.384). Ogni richiesta
genera esattamente 256 token (`ignore_eos`), così le esecuzioni sono
confrontabili. Ogni prompt inizia con un'etichetta univoca, quindi i risultati
sono **a cache fredda**. Nelle sessioni OpenCode reali il prompt di sistema e le
definizioni dei tool si ripetono, e la prefix cache di solito rende il tempo al
primo token migliore di questo caso peggiore.

Per ogni livello e tipo di prompt il benchmark riporta:

- numero di richieste, successi ed errori, raggruppati per tipo di errore;
- token medi in ingresso e in uscita, contati dal server;
- tempo totale, richieste al secondo, token in uscita al secondo e token totali
  al secondo;
- tempo al primo token (TTFT) come mediana, P95 e P99, e latenza complessiva
  come mediana e P95;
- tempo medio per token in uscita;
- preemption avvenute durante il livello, lette da `/metrics`;
- picco di uso della KV cache e picchi di richieste in esecuzione e in attesa,
  campionati ogni secondo.

I risultati finiscono in `bench-results/<timestamp UTC>/` (ignorato da Git) come
`summary.csv`, `summary.json` (con i parametri usati) e `requests.jsonl`. I
percentili sono calcolati per interpolazione lineare tra i ranghi più vicini.

Per confrontare i profili eseguite lo stesso benchmark dopo ogni cambio di
profilo. Quando condividete i risultati indicate profilo, digest
dell'immagine, revisione del modello e versione di DGX OS, e presentateli come
misure, non come garanzie.

### Simulazione di classe

`bench.sh` misura la capacità grezza con richieste sintetiche indipendenti. Una
classe di studenti che lavora con OpenCode si comporta in modo diverso, e
`scripts/bench-classroom.sh` la simula:

- Ogni utente simulato (40 per impostazione predefinita) lavora su un piccolo
  progetto Java client-server, una chat tenuta in memoria. Riceve un esercizio
  di laboratorio e poi fino a tre richieste successive, come "esegui i test e
  correggi gli errori".
- Risponde il **modello vero**, con un prompt di sistema da agente e tool nello
  stile di OpenCode (`read`, `write`, `edit`, `bash`, `glob`, `grep`, `list`).
  Legge i file, scrive e modifica codice, lancia build e test. I tool agiscono
  sul progetto in memoria; build e test richiedono 3–12 s di tempo simulato, e
  la prima esecuzione dei test di una sessione fallisce nel 30 % dei casi.
- Il contesto cresce a ogni passo, come in una sessione reale, e viene
  compattato vicino al limite di contesto del client.
- Gli utenti entrano in un arco di due minuti, fanno pause di 20–90 s tra una
  richiesta e l'altra e riprovano dopo un HTTP 429 o 503.
- Ogni utente ha la propria chiave API (`--api-keys-file`), quindi i limiti per
  utente del proxy agiscono come in aula.

L'output del modello e il carico sul server sono reali; file system, build e
studenti sono simulati. Per il carico più fedele, registrate il prompt di
sistema e le definizioni dei tool di una sessione OpenCode reale e passateli
con `--prompt-file`.

Lanciatela da una postazione, attraverso il proxy, così la rete entra nella
misura. Il simulatore richiede solo Python, quindi gira in un container
standard. Copiate `api-keys.csv` e, per una CA privata, `ca.pem` in `keys/` (Git
li ignora entrambi), poi:

```bash
mkdir -p bench-results keys
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD":/codendum -w /codendum python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f \
  python3 scripts/lib/codendum.py classroom --base-url https://llm.lab.example:8443 \
  --api-keys-file keys/api-keys.csv --cacert keys/ca.pem --users 40 --duration 900 --yes
```

Nello stesso momento registrate il lato server sull'host GB10:

```bash
scripts/metrics.sh --watch 5 --json > bench-results/server-timeline.jsonl
```

Il simulatore riporta:

- richieste ed errori, e i nuovi tentativi dopo un 429 o un 503;
- tempo al primo token e durata delle richieste (P50 e P95);
- velocità di scrittura per utente;
- tempo del modello per richiesta, cioè l'attesa che lo studente percepisce
  sommando tutti i passi con i tool;
- dimensione dei prompt, compattazioni ed errori dei tool.

Sull'host aggiunge i picchi di richieste attive e in attesa, l'uso della KV
cache, le preemption e la percentuale di riuso della prefix cache, se `/metrics`
è raggiungibile. I risultati vanno in `bench-results/classroom-<timestamp UTC>/`.

### Misure su un GB10

**Configurazione.** Misure del 2026-09-29 su una Lenovo ThinkStation PGX:

- NVIDIA GB10, 128 GB, DGX OS 7.2.3, driver 580.178.04;
- vLLM 0.30.0, con immagine e modello fissati come in `.env.example`;
- OpenCode 2.0.19 come client di riferimento.

Gli studenti simulati giravano in Docker su una postazione remota collegata
tramite VPN WireGuard, con un tempo di andata e ritorno di circa 100 ms, quindi
le latenze comprendono quella rete.

**Avvio.**

- vLLM ha riportato 62,4 GiB di KV cache: 1.362.640 token, sufficienti per 20,8
  contesti completi da 64K contemporanei.
- Il riavvio con il modello già nella cache locale ha richiesto circa 5
  minuti, per lo più spesi a caricare i pesi.

**Taratura.** Una sessione OpenCode reale su un esercizio ha fatto 33 chiamate
al modello in 63 s:

- 1 richiesta per il titolo;
- 20 passi dell'agente principale;
- 12 passi di un sotto-agente di esplorazione.

I prompt sono cresciuti da 6,8K a 11K token. Con uno o due utenti il tempo al
primo token era di circa 0,4 s e ogni utente riceveva circa 31 token di output
al secondo. Una richiesta di codice si completava in 1,7–2,2 minuti.

**40 studenti simulati.** La simulazione ha usato il prompt di sistema e i tool
registrati da OpenCode 2.0.19. Ogni studente ha fatto 4 richieste, con pause di
20–90 s. La finestra per le nuove richieste era di 15 minuti, seguita dal tempo
necessario a chiudere quelle in corso.

| | `classroom-64k` (16 attive) | `classroom-64k-high-concurrency` (24 attive) |
| --- | ---: | ---: |
| Output del server a regime | ~110 tok/s | ~140–150 tok/s |
| Tempo al primo token, P50 / P95 | 17,5 / 60 s | 10,6 / 34 s |
| Velocità di scrittura per utente, P50 | 7,2 tok/s | 6,1 tok/s |
| Tempo per completare una richiesta di codice, P50 / P95 | 11 / 22 min | 8,3 / 18 min |
| Richieste di codice completate | 45 | 64 |
| Picco di richieste in attesa | 24 | 15 |
| Picco di uso della KV cache | 8,5 % | 11,5 % |
| Preemption / errori del server | 0 / 0 | 0 / 0 |
| Riuso della prefix cache | 98,2 % | 98,2 % |

Cosa mostrano i numeri:

- **Il servizio è rimasto stabile.** Nessun errore del server, nessuna
  preemption, nessuna pressione sulla memoria dell'host. Circa l'1 % delle
  richieste è fallito sul percorso VPN e non ha mai raggiunto il proxy.
- **Il limite è la velocità di generazione, non la memoria.** I contesti degli
  agenti sono rimasti tra 7K e 19K token, quindi la KV cache non ha mai
  superato il 12 %. Ammettere più richieste insieme ha aumentato l'output
  totale del server. La memoria consentirebbe anche più di 24 richieste attive,
  ma non è stato misurato.
- **La prefix cache è indispensabile.** Il 98 % dei 7 milioni di token di
  prompt è stato servito dalla cache.
- **Pianificate la capacità in richieste di codice all'ora.** Una richiesta di
  codice ha prodotto circa 2.700 token di output in circa 15 chiamate al
  modello. A 110–150 token al secondo, un GB10 completa circa 150–200 richieste
  di questo tipo all'ora per tutta la classe: quattro o cinque per studente
  all'ora con 40 studenti. La simulazione, con pause inferiori a 90 s, chiede
  più di così, quindi le richieste finiscono in coda. Una classe che chiede
  meno aspetta meno.

<!-- section: profiles -->
## 8. Profili

| Profilo | `max-model-len` | `max-num-seqs` | `max-num-batched-tokens` | `gpu-memory-utilization` | KV cache | `limit.context` di OpenCode |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| `classroom-64k` (predefinito) | 65536 | 16 | 8192 | 0.80 | FP8 | 65536 |
| `classroom-64k-high-concurrency` | 65536 | 24 | 8192 | 0.80 (iniziale) | FP8 | 65536 |
| `deep-128k` | 131072 | 8 | 8192 | 0.80 | FP8 | 131072 |

- **`classroom-64k`** è il profilo predefinito per un'aula o un team. Secondo
  la stima precedente, tutte le 16 richieste attive possono essere alla
  lunghezza massima nello stesso momento.
- **`classroom-64k-high-concurrency`** ammette più richieste simultanee. Conta
  sul fatto che la maggior parte delle richieste sia molto più corta di 64K;
  molte richieste lunghe insieme causano preemption. Le sessioni agentiche di
  OpenCode rientrano in questo schema (7–19K token): nella simulazione con 40
  studenti questo profilo ha completato il 42 % di richieste di codice in più di
  `classroom-64k`, senza preemption.
- **`deep-128k`** è pensato per pochi utenti che lavorano su contesti ampi. I
  prefill molto lunghi sono lenti e consumano molta memoria, e tutti i client
  OpenCode devono passare a 131072 nello stesso momento.

Cambiate profilo con un riavvio controllato:

```bash
scripts/start-vllm.sh --profile classroom-64k-high-concurrency --replace --wait
```

Per rendere permanente la scelta impostate `CODENDUM_PROFILE` in `.env`. I
singoli valori si possono sovrascrivere in `.env` (ad esempio
`CODENDUM_MAX_NUM_SEQS=20`); vedete i file dei profili in
[`config/profiles/`](../../config/profiles/).

`0.80` per `gpu-memory-utilization` lascia margine al sistema operativo e ai
processi dell'host, che condividono la stessa memoria. NVIDIA indica
l'allocazione aggressiva sui sistemi a memoria unificata come causa di errori di
memoria esaurita. Alzate il valore solo se, sotto carico, `scripts/metrics.sh`
mostra ampia memoria host disponibile.

<!-- section: troubleshooting -->
## 9. Risoluzione dei problemi

**Diagnostica**

| Sintomo | Causa probabile e azione |
| --- | --- |
| Il container termina all'avvio per memoria esaurita (`NV_ERR_NO_MEMORY`) | Abbassate `CODENDUM_GPU_MEMORY_UTILIZATION` (0.75, poi 0.70) o `CODENDUM_MAX_NUM_SEQS` e fermate gli altri carichi. Come rimedio su DGX Spark, NVIDIA documenta lo svuotamento della page cache: `sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'`. Vedete anche la issue vLLM [#56824](https://github.com/vllm-project/vllm/issues/56824). |
| Il sistema non risponde durante prefill molto lunghi | Sono state segnalate instabilità con prefill da 128K token su DGX Spark ([dgx-spark-playbooks#97](https://github.com/NVIDIA/dgx-spark-playbooks/issues/97)). Evitate `deep-128k` con molti utenti e mantenete `0.80`. |
| La qualità delle risposte peggiora, o gli output lunghi si ripetono | La KV cache FP8 può influire sulla qualità, e le indicazioni di vLLM per DGX Spark dicono di usarla solo se la pressione sulla memoria lo richiede e le verifiche di qualità sono superate. Confrontate con `CODENDUM_KV_CACHE_DTYPE=auto`, che raddoppia la memoria KV per token a 96 KiB, e riducete `max-num-seqs` di conseguenza. |
| Errori di kernel relativi al block scaling FP8 su SM 12.1 | Osservati con build CUDA 12.9 ([vllm#43367](https://github.com/vllm-project/vllm/issues/43367)). Usate un'immagine CUDA 13; quella predefinita lo è. |
| Testo `<tool_call>` nelle risposte; OpenCode non modifica mai i file | Il parsing dei tool è disattivato o errato: eseguite `scripts/smoke-test.sh`. Sul client verificate `capabilities.tools: true`, che il provider non si chiami `vllm` e che chiavi V1 e V2 non siano mescolate nello stesso provider. |
| HTTP 400 che cita la lunghezza massima del contesto | La conversazione supera `max-model-len`. Allineate `limit.context` di OpenCode al profilo e compattate o ricominciate la sessione. |
| Risposte lente per tutti | Controllate con `scripts/metrics.sh --watch 5` richieste in attesa, KV cache vicina al 100 % e preemption in crescita. Riducete concorrenza o contesto, oppure cambiate profilo. |
| Errori TLS in OpenCode | Impostate `NODE_EXTRA_CA_CERTS` sul certificato della CA interna. |
| `docker: permission denied` | Usate un account autorizzato a usare Docker. Su DGX OS l'aggiunta dell'utente al gruppo `docker` è facoltativa e concede un accesso equivalente a root. |
| `start-proxy.sh` segnala la porta come occupata | Un altro servizio usa la 8443. Cambiate la riga `listen` in `/etc/codendum/proxy/conf.d/codendum.conf` e la porta nel `baseURL` di ogni client, poi avviate di nuovo il proxy. |
| Runtime NVIDIA assente in `docker info` | Il passo di risoluzione indicato da NVIDIA è `sudo nvidia-ctk runtime configure --runtime=docker` seguito da `sudo systemctl restart docker`. Non reinstallate i driver. |

Per i codici HTTP 401, 403, 404, 429, 502 e 504 vedete
[la sezione sul proxy](#4-proxy-https).

**Log**

- vLLM: `docker logs codendum-vllm`. I prompt non vengono registrati, perché
  `--enable-log-requests` è disattivato per impostazione predefinita.
- nginx: `/etc/codendum/proxy/logs/codendum.access.log` registra ora,
  indirizzo del client, identificativo utente, percorso, stato e durate, mai le
  chiavi né il contenuto delle richieste. Gli errori vanno in
  `codendum.error.log` nella stessa directory. Con l'nginx della distribuzione
  la directory è `/var/log/nginx/`.

<!-- section: limits -->
## 10. Limiti e responsabilità

- **Nessuna quota di token per utente e nessuno scheduling equo.** nginx limita
  le richieste e vLLM le serve in ordine di arrivo. Vedete
  [Coda ed equità](#coda-ed-equità).
- **Nessun isolamento tra i repository degli utenti.** Il server del modello
  vede solo ciò che i client gli inviano e non conserva stato per utente oltre
  alla prefix cache condivisa. Isolare codice, credenziali e ambienti di build
  degli utenti è compito delle postazioni o degli ambienti di sviluppo.
- **Gli agenti eseguono comandi sulle postazioni.** OpenCode esegue i tool con
  i permessi dell'utente. Sandbox e politiche dei permessi sono responsabilità
  delle postazioni.
- **Un solo host, senza alta disponibilità.** Quando il GB10 o vLLM sono fermi,
  tutti gli utenti ne risentono.
- **Misurato su un solo GB10, con un modello e una versione del client.** Altri
  host, immagini, modelli o versioni di OpenCode cambiano i numeri. Misurate di
  nuovo con `bench.sh` e `bench-classroom.sh` dopo ogni aggiornamento.
- **L'output del modello va rivisto.** Il codice generato deve essere
  compilato, testato e rivisto da persone, e i progetti vanno costruiti in modo
  iterativo, non con un'unica generazione.
- **Regole e dati personali.** I prompt contengono il codice degli utenti, e i
  log di nginx contengono identificativi utente e orari. Il gestore è
  responsabile delle regole applicabili, ad esempio sulla protezione dei dati
  di studenti o dipendenti, e della conservazione dei log.

<!-- section: security -->
## Modello di sicurezza

Il modello di minaccia di Codendum in sintesi. Per segnalare vulnerabilità
vedete [SECURITY.md](../../SECURITY.md).

| Risorsa | Minacce | Mitigazioni in Codendum | Rischio residuo / compiti del gestore |
| --- | --- | --- | --- |
| Proxy e API | Accesso dall'esterno dell'organizzazione; uso di endpoint vLLM non documentati; esaurimento delle risorse | vLLM in ascolto solo su `127.0.0.1`; reti ammesse in nginx più firewall; TLS; solo `/v1/chat/completions` e `/v1/models` inoltrati, con i percorsi non normalizzati rifiutati; limiti per utente e globali; errori JSON senza dettagli interni; container del proxy con file system in sola lettura, capability minime e `no-new-privileges` | Vulnerabilità di nginx e vLLM (tenerli aggiornati); gli utenti autenticati possono comunque generare molto carico |
| Chiavi API | Fuga, condivisione, riuso dopo l'uscita di una persona | Chiavi casuali a 256 bit generate in locale; mappa delle chiavi e chiave TLS leggibili solo dall'account di servizio (permessi 0600); chiavi mai su riga di comando né in Git; i client le leggono da una variabile d'ambiente; rotazione e revoca modificando la mappa | Gli utenti possono condividere le chiavi; la tracciabilità si basa sui log di nginx; distribuite le chiavi in modo privato |
| Repository e prompt degli utenti | Divulgazione in transito o sul server | TLS; prompt non registrati da vLLM; nessuna persistenza oltre alle cache in memoria | La prefix cache è condivisa: in linea di principio i tempi di risposta potrebbero rivelare che qualcuno ha inviato di recente lo stesso prefisso. Se conta, aggiungete `--no-enable-prefix-caching` a `CODENDUM_VLLM_EXTRA_ARGS`, a scapito del throughput. La sicurezza delle postazioni è fuori ambito. |
| Cache e log | Conservazione non voluta di dati personali | La cache del modello contiene solo pesi pubblici; i log di nginx registrano identificativo utente, indirizzo, percorso, stato e tempi; i dati del benchmark sono sintetici | Definite la conservazione dei log; limitate l'accesso alla shell e a Docker sull'host (l'accesso a Docker equivale a root) |
| Catena di fornitura | Immagine, modello o action di CI manomessi | Immagini di vLLM e nginx fissate tramite digest; modello fissato per revisione; action della CI fissate a SHA completi e aggiornate da Dependabot; la CI usa un token in sola lettura e nessun segreto | Compromissione a monte prima del fissaggio; rivedete gli aggiornamenti |

<!-- section: components -->
## Componenti, versioni e licenze

Questo repository è distribuito con licenza Apache 2.0: vedete
[LICENSE](../../LICENSE) e [NOTICE](../../NOTICE). La licenza copre solo i file
del repository: script, configurazioni d'esempio e documentazione. Il
repository non contiene né ridistribuisce immagini container, pesi dei modelli
o software di terze parti. Il gestore li scarica separatamente, e sono soggetti
alle proprie licenze e condizioni, che il gestore deve verificare e rispettare.

| Componente | Versione fissata | Licenza | Dove gira |
| --- | --- | --- | --- |
| Codendum (questo repository) | 0.1.0 (non rilasciato) | Apache-2.0 | Host GB10, postazioni |
| Immagine container vLLM | `vllm/vllm-openai:v0.30.0@sha256:8a69ffad015f138d7170c4ddc429e230a3bc1c1719f67e14324749df200a4b90` (indice; il manifest linux/arm64 è `sha256:4864d466…`), CUDA 13.0.2 | vLLM: Apache-2.0; l'immagine include software di terze parti (ad esempio il runtime CUDA e PyTorch) con le rispettive licenze | Host GB10 |
| Modello | `Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8` alla revisione `dcaee4d4dfc5ee71ad501f01f530e5652438fde0` | Apache-2.0 secondo la scheda del modello; verificate il file di licenza del modello | Host GB10 |
| OpenCode | V2 (2.0.x); 1.x con l'esempio V1 | MIT | Postazioni |
| Immagine container nginx | `nginx:1.30.5-alpine@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94` | nginx: BSD-2-Clause; la base Alpine include pacchetti con le rispettive licenze | Host GB10 |
| Docker Engine, NVIDIA Container Toolkit | Preinstallati con DGX OS | Apache-2.0 | Host GB10 |
| `actions/checkout` | v7.0.1 @ `3d3c42e5aac5ba805825da76410c181273ba90b1` | MIT | Solo CI |
| Codice di condotta | Contributor Covenant 3.0 | CC BY-SA 4.0 | Documentazione |

<!-- section: versioning -->
## Versioni e compatibilità

Codendum segue il [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
L'interfaccia pubblica comprende:

- nomi e opzioni degli script;
- i nomi delle variabili `CODENDUM_*`;
- i nomi dei profili;
- la struttura delle configurazioni d'esempio.

Le modifiche sono registrate in [CHANGELOG.md](../../CHANGELOG.md).

| Ambito | Stato |
| --- | --- |
| Test offline (CI) | Ubuntu 24.04 su x64 e ARM64: lint, test degli script contro un finto vLLM, test del proxy (container nginx e pacchetto della distribuzione), controlli su documentazione e segreti |
| Host di destinazione | DGX OS 7 su NVIDIA GB10. Provato su una Lenovo ThinkStation PGX (DGX OS 7.2.3) il 2026-09-29: avvio, smoke test, proxy, una sessione OpenCode reale e simulazioni con 40 utenti |
| vLLM | Pensato per v0.30.0. `qwen3_coder` richiede ≥ 0.25 (le versioni precedenti avevano un'implementazione diversa del parser); `CODENDUM_MAX_NUM_QUEUED_REQS` richiede ≥ 0.29 |
| OpenCode | Configurazione nativa V2; formato V1 fornito a parte |

<!-- section: development -->
## Sviluppo

La suite di test offline richiede Linux con bash ≥ 4.4, Python 3, curl, OpenSSL
e Git, più ShellCheck, yamllint, Docker e nginx per l'esecuzione completa. Non
servono GPU né pesi del modello; il test del container del proxy scarica
l'immagine nginx fissata.

```bash
tests/run.sh
```

Vedete [CONTRIBUTING.md](../../CONTRIBUTING.md) per il flusso di lavoro e lo
stile, [docs/TRANSLATING.md](../TRANSLATING.md) per le traduzioni e
[docs/MAINTAINING.md](../MAINTAINING.md) per rilasci e aggiornamenti.
Partecipando accettate il [Codice di condotta](../../CODE_OF_CONDUCT.md), in
inglese.

<!-- section: references -->
## Riferimenti

- NVIDIA, DGX Spark: <https://www.nvidia.com/it-it/products/workstations/dgx-spark/> e la panoramica hardware <https://docs.nvidia.com/dgx/dgx-spark/hardware.html>
- NVIDIA, vLLM su DGX Spark (playbook): <https://build.nvidia.com/spark/vllm/instructions>
- NVIDIA, runtime container per Docker su DGX Spark: <https://docs.nvidia.com/dgx/dgx-spark/nvidia-container-runtime-for-docker.html>
- NVIDIA, note di rilascio del container vLLM: <https://docs.nvidia.com/deeplearning/frameworks/vllm-release-notes/>
- vLLM su DGX Spark (blog di vLLM): <https://vllm.ai/blog/2026-06-01-vllm-dgx-spark>
- Qwen, checkpoint FP8: <https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8>
- vLLM, opzioni di `serve`: <https://docs.vllm.ai/en/latest/cli/serve/>
- vLLM, tool calling: <https://docs.vllm.ai/en/latest/features/tool_calling/>
- vLLM, metriche: <https://docs.vllm.ai/en/latest/usage/metrics/>
- vLLM, sicurezza: <https://docs.vllm.ai/en/latest/usage/security/>
- OpenCode V2, modelli e server locali: <https://opencode.ai/v2/docs/models>
- OpenCode V2, provider: <https://opencode.ai/v2/docs/providers>
- Open Source Guides, avviare un progetto: <https://opensource.guide/starting-a-project/>
- GitHub, licenze di un repository: <https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository>
- Licenza Apache 2.0: <https://choosealicense.com/licenses/apache-2.0/>
- GitHub Actions, uso sicuro: <https://docs.github.com/en/actions/reference/security/secure-use>
- GitHub, segnalazione privata delle vulnerabilità: <https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository>

## Autore

Codendum è creato e mantenuto da [Stefano Noferi](https://noferi.it/)
(GitHub: [`stefanoferi`](https://github.com/stefanoferi)).
