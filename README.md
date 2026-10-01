# Session Board Portable

Una board autonoma per seguire le sessioni tmux di un utente Linux: accesso con nome utente, password e codice 2FA, elenco sessioni e pannelli, lettura dell'output e metriche della macchina. L'invio di testo ai pannelli è acceso di serie; si spegne con `BOARD_ALLOW_SEND=0`.

Gli stati `working`, `waiting`, `blocked`, `idle` e `done` arrivano da eventi opzionali; un pannello aperto non dimostra che un agente stia lavorando. È disponibile anche una diagnostica MCP opzionale. La distribuzione comprende abbonamenti Claude, aggiunta e login guidato, quote disponibili Claude/Codex e switch della conversazione tra abbonamenti Claude. Esclude ecosistema, Odoo, Telegram e i dati della board originale. Non include credenziali o trascrizioni: i profili si configurano sulla macchina di destinazione.

## Avvio locale

Requisiti: Linux, Python 3.10 o superiore, tmux. Su una nuova Ubuntu 24.04 si possono installare i requisiti di sistema con `sudo apt-get install python3-venv tmux`.

Dopo aver estratto l'archivio, eseguire come **lo stesso utente delle sessioni tmux**:

```sh
cd session-board
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python deploy/set-password.py
.venv/bin/python deploy/setup-2fa.py --username admin
BOARD_BIND=127.0.0.1:18099 ./run.sh
```

Aprire `http://127.0.0.1:18099`, username `admin`, con la password appena scelta. Al primo accesso inserire anche il codice di attivazione mostrato nel terminale da `setup-2fa.py`: vale 15 minuti ed è monouso. Inquadrare il QR con l'app di autenticazione, confermare con il suo codice temporaneo e salvare i dieci codici di recupero. La board si apre dopo la conferma. L'esempio usa la porta 18099 per facilitare la prova accanto a una board esistente. Il valore predefinito di `run.sh` è `127.0.0.1:8099`.

Lo script chiede due volte la password senza mostrarla e salva solo un hash scrypt in `~/.config/session-board/password.hash`, con permessi `0600`. Richiede almeno 12 caratteri e rifiuta terminali che non possono nascondere l'input. Non passare password in argomenti, file `.env`, log o user-data. Per cambiarla, eseguire di nuovo lo script e riavviare soltanto la propria istanza portable.

Una sessione di prova su una macchina destinata al collaudo si crea con `tmux new-session -d -s portable-demo`. La board vede il socket tmux dell'utente che la esegue; un account diverso non vede le sessioni esistenti. Non sono necessari container, privilegi root o mount del filesystem dell'host.

## Configurazione

| Variabile | Valore predefinito e funzione |
| --- | --- |
| `BOARD_BIND` | `127.0.0.1:8099`, indirizzo di Gunicorn |
| `BOARD_USERNAME` | `admin`, username del login |
| `BOARD_PASSWORD_HASH_FILE` | Con `run.sh`, `~/.config/session-board/password.hash`; contiene l'hash generato dallo script |
| `BOARD_PASSWORD_HASH` | Alternativa al file, hash scrypt fornito dall'ambiente; usare preferibilmente il file |
| `BOARD_SECRET_KEY` | Se assente, chiave casuale in memoria; a ogni riavvio occorre rifare il login |
| `BOARD_STATE_DIR` | `~/.local/state/session-board`, database SQLite degli stati ricevuti |
| `BOARD_MFA_STATE_DIR` | `BOARD_STATE_DIR/auth`, stato privato del secondo fattore |
| `BOARD_TMUX_SOCKET` | `default`, nome del socket tmux, equivalente a `tmux -L NOME` |
| `BOARD_ALLOW_SEND` | `1`: invio di messaggi, aggiunta/login degli abbonamenti e switch sono accesi di serie; con `0` la board è in sola osservazione |
| `BOARD_AUDIO_ENABLED` | `0`; con `1` abilita la dettatura locale nella chat, se l'invio è abilitato |
| `BOARD_AUDIO_PYTHON` | Interprete dell'app; indicare il percorso assoluto del virtualenv con `requirements-audio.txt` |
| `BOARD_WHISPER_MODEL` | Assente; directory assoluta del modello faster-whisper già scaricato |
| `BOARD_WHISPER_LANGUAGE` | `it`; codice lingua oppure `auto` per il riconoscimento automatico |
| `BOARD_ACCOUNTS_ENABLED` | `1`; con `0` esclude completamente il modulo abbonamenti |
| `BOARD_ACCOUNTS_HOME` | Home dell’utente della board; contiene i profili Claude e Codex |
| `BOARD_CLAUDE_BIN` | CLI `claude` trovata nel PATH; si può indicare il percorso assoluto |
| `BOARD_CODEX_BIN` | CLI `codex` trovata nel PATH; solo lettura di account e quote |
| `BOARD_COOKIE_SECURE` | `0` per HTTP su localhost attraverso un tunnel SSH; usare `1` con HTTPS |
| `BOARD_MCP_ENABLED` | `0`, diagnostica MCP opzionale |
| `BOARD_INGEST_TOKEN` | Assente: ricezione eventi disattivata; se presente autorizza solo l'endpoint eventi |

Non impostare insieme le due variabili dell'hash: il file ha precedenza. Senza hash valido l'applicazione non parte. Il login usa cookie HttpOnly, SameSite Strict, protezione CSRF e limite dei tentativi. L'istanza usa un processo Gunicorn con otto thread, coerentemente con il limite dei tentativi in memoria. Non aumentare il numero dei worker senza introdurre uno stato condiviso per autenticazione e limiti.

Il contenuto dei terminali è accessibile dopo la verifica di entrambi i fattori. L'invio, acceso di serie, dà anche la possibilità di eseguire comandi nella shell dei pannelli; con `BOARD_ALLOW_SEND=0` la board resta in sola osservazione. 

## Secondo fattore e recupero

Il login richiede prima nome utente e password, poi un codice TOTP di sei cifre dall'app di autenticazione. La sessione dura 12 ore; la fase intermedia scade dopo cinque minuti e non autorizza API o terminali. Un codice già accettato non può essere riutilizzato, anche dopo un riavvio.

Se il telefono non è disponibile, inserire uno dei codici di recupero nel campo del secondo fattore. Ogni codice si può usare una volta sola e richiede comunque nome utente e password. I codici di recupero non disattivano il 2FA.

`setup-2fa.py` deve essere eseguito dallo stesso utente del servizio, in un terminale interattivo. Se si personalizza la directory dello stato, passare `--state-dir /percorso/dello/stato/auth`; il nome utente deve coincidere con `BOARD_USERNAME`. Una configurazione iniziale abbandonata richiede un nuovo codice di attivazione. Il comando rifiuta di sovrascrivere un fattore già registrato.

Il segreto TOTP è cifrato in `mfa.sqlite3`, con chiave separata `mfa.key` e file privati. Conservare un backup protetto dell'intera directory del secondo fattore: database, chiave e marker di inizializzazione sono necessari insieme. La perdita o corruzione di un componente blocca l'accesso; non si riattiva il login con la sola password. I file di stato e i segreti non sono inclusi nell'archivio portable.

Connettersi via HTTPS quando la board è raggiunta da un'altra macchina; HTTP locale è destinato a loopback o tunnel SSH. Il QR è generato localmente e non viene inviato a servizi esterni. L'app di autenticazione e il server devono avere l'orologio sincronizzato.

## Abbonamenti e terminale

Il selettore **Schede / Elenco** cambia la disposizione delle sessioni e ricorda la scelta sul dispositivo. L'elenco mostra righe compatte con stato, nome, progetto, preferito e accessi a chat e terminale. Ricerca e filtri continuano a funzionare in entrambe le viste.

Nell'elenco sono visibili anche **token del contesto** e **abbonamento** del pannello attivo. I metadati Claude vengono ricavati dal processo e dalla conversazione verificati; non sono il consumo cumulativo dell'abbonamento. Si aggiornano circa ogni quindici secondi mentre l'elenco è visibile, con al massimo due richieste contemporanee. Dati non verificabili, compresi attualmente quelli Codex, appaiono come **n/d**. Il link **Terminale ↗** apre in una nuova scheda il pannello portable a schermo intero, mantenendo l'identità del pannello: un link a una sessione sostituita non apre quella nuova per errore.

Per abilitare le azioni nell’avvio locale:

```sh
BOARD_ALLOW_SEND=1 BOARD_BIND=127.0.0.1:18099 ./run.sh
```

Il CLI Claude deve essere già installato e accessibile allo stesso utente della board. Il primo account si collega dal proprio terminale con `claude auth login --claudeai`. La scheda **Abbonamenti** scopre il principale e i profili `.claude-<nome>` presenti nella home. **Aggiungi abbonamento** crea uno slot secondario e avvia il login: aprire il link di autorizzazione, completare l’accesso nel browser e incollare il codice nella board. Chiudere il dialog annulla il login ancora aperto. Le credenziali sono gestite dal CLI sul nuovo host; non entrano nell’archivio.

I nuovi slot condividono soltanto i transcript e il registro delle sessioni tramite collegamenti locali. Impostazioni, connettori cloud, hook globali ed ecosistema non vengono copiati dal sistema originale. Gli eventuali MCP necessari a un secondario vanno configurati su quel profilo. Il principale usa la propria configurazione nativa senza forzare `CLAUDE_CONFIG_DIR`.

Dalla scheda della sessione, **Chat** apre la conversazione Claude o Codex già in corso con messaggi separati, testo formattato e pulsanti per copiare messaggi e codice. Le tabelle Markdown mantengono intestazioni e colonne e, quando sono larghe, scorrono orizzontalmente sul telefono. Non occorre avviare una nuova sessione. Le schede **Chat** e **Terminale** condividono il pannello e conservano la bozza passando da una vista all'altra. Il campo di scrittura accetta più righe e resta accessibile dal telefono con la tastiera aperta. **Apri sessione** mantiene l'accesso diretto all'output del terminale.

**Espandi terminale** usa quasi tutta l’altezza dello schermo per leggere l’output, conservando bozza, allegati e posizione. **Torna ai controlli**, oppure Esc, ripristina la vista normale. Durante la lettura espansa i tasti restano locali: non vengono inviati comandi alla sessione. La funzione è disponibile anche entrando dal collegamento al terminale a schermo intero.

La chat si aggiorna ogni cinque secondi e legge solo il transcript verificato per il processo scelto: ultimi 100 messaggi entro 2 MiB, massimo 24.000 caratteri per messaggio. Una cronologia ridotta viene segnalata. Le azioni dell'agente compaiono in gruppi richiudibili con emoji, in ordine fra i messaggi: letture, modifiche, ricerche e comandi. I gruppi restano aperti durante gli aggiornamenti se aperti dall'utente. Anche i dettagli di ogni singola azione sono chiusi inizialmente: **Dettagli** li apre e conserva l'apertura durante gli aggiornamenti. Ogni azione mostra il nome reale dello strumento e, quando disponibile, il comando con i suoi argomenti o il percorso del file. I dettagli sono copiabili; il lettore oscura le credenziali riconosciute e omette script incorporati e dettagli non interpretabili prima della visualizzazione. I dettagli oltre 4.000 caratteri sono abbreviati con indicazione esplicita. Ragionamento, output grezzi degli strumenti, patch e messaggi interni restano esclusi. Le immagini incorporate nella conversazione vengono mostrate come anteprime apribili in una scheda separata; il testo dell’allegato resta disponibile per riconoscere la consegna. Per conversazioni non verificabili viene mostrato il motivo e resta disponibile il terminale. La lettura funziona anche in modalità solo osservazione e con la gestione abbonamenti disabilitata. L'invio continua a richiedere `BOARD_ALLOW_SEND=1`: il pulsante **Invia** spedisce direttamente alla sessione selezionata, senza una seconda finestra di conferma, e mostra l'esito.

Sono visualizzabili immagini PNG/JPEG/WebP/GIF incorporate nei messaggi Claude, compresi i risultati immagine degli strumenti; questi ultimi sono indicati come **Immagine dello strumento**. Sono supportati anche gli allegati immagine incorporati nei messaggi utente Codex. Il caricamento richiede la stessa autenticazione e la verifica della stessa conversazione; le immagini vengono convertite in PNG senza metadati. Non vengono letti percorsi di file o recuperati URL arbitrari. Il limite è di 20 immagini recenti, fino a 5 per record, entro la stessa finestra di lettura di 2 MiB: un record immagine oltre quella finestra non è disponibile. I formati non supportati e i limiti incontrati vengono indicati nella chat.

Gli invii dalla chat compaiono immediatamente con lo stato **Invio in corso** e poi **In coda**, in attesa che il CLI li registri nella conversazione. La ricevuta verificata del server permette di sostituire la bolla provvisoria senza duplicarla o confonderla con un messaggio precedente uguale. Errori e consegne incerte restano distinti; non vengono effettuati reinvii automatici. Questi stati provvisori sono conservati nella scheda del browser durante il passaggio fra sessioni, fino alla riconciliazione; non costituiscono una coda persistente dopo la chiusura o il ricaricamento della pagina.

Le richieste di approvazione riconosciute nel terminale corrente appaiono nella chat con i pulsanti delle scelte effettive. **Operazione richiesta** mostra sopra i pulsanti il comando da autorizzare, anche quando proviene da un sottoagente. Il riepilogo applica le stesse protezioni dei dettagli delle azioni; per un commit con messaggio generato da uno script indica `git commit -m [script omesso]`. Se il comando non è verificabile, invita ad aprire il terminale. Il server ricontrolla pannello, processo, conversazione e richiesta prima di inviare ogni tasto; una richiesta scaduta o cambiata richiede una nuova lettura. Nessuna approvazione automatica e nessuna conferma browser aggiuntiva. I menu non riconoscibili indicano di usare **Terminale**. In sola lettura i pulsanti non inviano risposte. La bozza viene conservata; sugli schermi bassi il modulo di scrittura lascia spazio alla richiesta finché resta aperta.

Anche i menu a scelta singola di Codex, come l'avviso sui limiti di utilizzo o la scelta del modello, mostrano da **2 a 9 pulsanti numerati**, con etichette e descrizioni originali. L'opzione corrente porta l'indicazione **Selezionata nel terminale**. Il clic naviga fino alla scelta e la conferma una sola volta, dopo aver verificato che il menu non sia cambiato. Le approvazioni Claude e Codex continuano a mostrare tutte le alternative riconosciute, anche quando sono più di due. I moduli con più domande, selezioni multiple o campi **Other** da compilare richiedono ancora il terminale.

Per Claude la lettura riconosce anche il registro nativo del CLI, verificando PID, avvio del processo, macchina e namespace: funziona con più conversazioni nella stessa cartella e con sessioni avviate prima della board. Per Codex usa il rollout principale aperto in scrittura dal processo, escludendo i transcript dei subagenti e rispettando l'eventuale `CODEX_HOME` del processo. Non seleziona il file più recente; più conversazioni principali aperte contemporaneamente restano ambigue. Il riconoscimento per la chat non modifica le regole più restrittive dello switch fra abbonamenti.

Questo terminale non richiede ttyd e non è un emulatore interattivo completo. Lo switch riguarda un singolo processo Claude fermo: mantiene cartella, UUID della conversazione e gli altri pannelli. Non sceglie il transcript più recente del progetto. Quando l’associazione non è dimostrabile e ci sono più transcript, richiede una selezione esplicita; se non può verificare lo stato del processo, blocca lo switch prima del riavvio.

Nel terminale rapido e nella chat si possono incollare più immagini con **Ctrl/Cmd+V**, oppure selezionarle insieme con **Allega immagine**. Ogni nuova selezione aggiunge le immagini alla bozza, con anteprime rimovibili singolarmente; il testo è facoltativo quando sono presenti allegati. Il pulsante **Invia** passa direttamente l’allegato al composer del CLI Claude o Codex già aperto nel pannello. Le shell e i pannelli con processi non riconosciuti non accettano immagini. Cambiare pannello o chiudere il terminale cancella la bozza locale. Le operazioni che riavviano una sessione, come lo switch di abbonamento, mantengono la propria conferma.

Sono consentite fino a **5 immagini per invio**, PNG/JPEG/WebP/GIF, fino a **10 MiB ciascuna e 20 MiB complessivi**. Il server verifica il contenuto e converte il primo fotogramma in PNG senza metadati, rispettando l’orientamento EXIF. I file sono privati, sotto `BOARD_STATE_DIR/terminal-images`, con quota complessiva di 256 MiB. Scadono dopo sette giorni; la pulizia dei file scaduti avviene al caricamento successivo. Il limite di decodifica è 25 milioni di pixel complessivi, anche per le animazioni.

L’invio convalida tutte le immagini prima di iniziare e attende che il CLI mostri ogni allegato nel composer. I messaggi della chat e quelli multilinea vengono incollati, verificati nel campo di scrittura e poi inviati con un solo Enter separato. Il risultato è confermato quando il campo si svuota; in caso contrario compare **Invio da verificare**, senza ripetere Enter automaticamente. Se il caricamento non viene confermato o la connessione si interrompe, verificare l’output del terminale prima di riprovare: un’immagine già incollata può essere rimasta nella bozza del CLI. Un eventuale reverse proxy deve consentire almeno **21 MiB** per le richieste multipart; il limite applicativo resta 10 MiB per immagine e 20 MiB per invio.

Lo switch preserva le opzioni supportate di modello, permessi, directory aggiuntive e MCP. Una configurazione personalizzata passata tramite `--settings` viene rifiutata esplicitamente se non è quella degli hook generati dalla board, evitando di perderne silenziosamente le impostazioni. Gli account non collegati, le sessioni al lavoro e i processi misti Claude/Codex non sono destinazioni valide. Logout e rimozione richiedono che nessuna sessione viva utilizzi il profilo. La ripresa può comunque mostrare richieste del CLI nel terminale.

Le quote sono osservazioni: dati assenti o scaduti vengono indicati come tali. Claude legge l’endpoint dei consumi del proprio profilo senza rinnovare token. Codex espone solo stato e quote leggibili localmente o tramite `account/read` e `account/rateLimits/read`, senza avviare turni. Non c’è gestione di account multipli o switch Codex.

Sulla **nuova VM** installata da cloud-init, abilitare le azioni con un override della sua unità:

```ini
[Service]
Environment=BOARD_ALLOW_SEND=1
```

Crearlo con `sudo systemctl edit session-board.service`, quindi riavviare quell’istanza. La home di `board` deve essere scrivibile per profili e transcript; il codice in `/opt/session-board` resta di proprietà root. Il PATH dell’unità include `/home/board/.local/bin`, dove può trovarsi il CLI installato dall’utente.

## Dettatura locale nella chat

Il pulsante **🎙️ Registra messaggio** registra dal microfono del dispositivo; un secondo tocco ferma la registrazione e avvia Whisper sulla macchina della board. La trascrizione viene aggiunta alla bozza corrente: si può correggerla e inviarla con **Invia**. Non viene inviata automaticamente alla sessione. **Cmd/Ctrl + Maiusc + Spazio** avvia o ferma il microfono; **Cmd/Ctrl + Invio** invia la bozza. Invio semplice va a capo. Le scorciatoie funzionano solo nella chat e rispettano i pulsanti disabilitati. Il browser deve autorizzare il microfono e usare HTTPS oppure localhost.

Prima del caricamento, l’audio completato viene salvato nel browser tramite IndexedDB. Se la rete cade durante la dettatura, la registrazione continua; dopo Stop resta in attesa e la trascrizione viene ritentata quando la stessa chat è di nuovo verificata e visibile. Dopo una chiusura, riaprire la chat per recuperarlo; nei browser privi di Web Locks premere **Riprova trascrizione**. Le registrazioni di altre sessioni non vengono inserite nel pannello corrente: il menu **Altri audio salvati** permette di scaricarle o eliminarle anche se il processo originario è stato sostituito. Una trascrizione già completata trovata al riavvio propone il recupero esplicito; le bozze di testo vengono conservate localmente.

La coda contiene fino a 10 audio e 50 MiB, senza cancellazioni automatiche delle registrazioni pendenti. Si può riprovare, scaricare o eliminare l’audio. Se il salvataggio locale non è disponibile, un messaggio lo segnala: in quel caso scaricare l’audio prima di chiudere la pagina. La chiusura forzata del browser durante una registrazione ancora in corso non garantisce il recupero; cancellare i dati del sito elimina le copie locali.

La funzione è opzionale: il pacchetto base non installa Whisper né scarica modelli. Per prepararla, dalla cartella estratta e come utente del servizio:

```sh
python3 -m venv .venv-audio
.venv-audio/bin/python -m pip install -r requirements-audio.txt
.venv-audio/bin/python - <<'PY'
from pathlib import Path
from huggingface_hub import snapshot_download
destination = Path.home() / '.local/share/session-board/whisper-small'
snapshot_download('Systran/faster-whisper-small',
                  revision='536b0662742c02347bc0e980a01041f333bce120',
                  local_dir=str(destination),
                  allow_patterns=['model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt'])
print(destination)
PY
BOARD_ALLOW_SEND=1 BOARD_AUDIO_ENABLED=1 \
BOARD_AUDIO_PYTHON="$PWD/.venv-audio/bin/python" \
BOARD_WHISPER_MODEL="$HOME/.local/share/session-board/whisper-small" \
BOARD_WHISPER_LANGUAGE=it ./run.sh
```

Solo la preparazione richiede Internet. Durante la trascrizione il worker usa esclusivamente il modello presente nella directory configurata, senza API esterne, download automatici o fallback cloud. Il modello Small richiede circa 500 MB su disco; CPU con supporto alle librerie CTranslate2, due thread e memoria sufficiente entro il limite di 4 GiB del worker. Per lingue diverse usare il relativo codice o `auto`. [Documentazione faster-whisper](https://github.com/SYSTRAN/faster-whisper).

Nel servizio systemd impostare le stesse quattro variabili audio, con percorsi assoluti accessibili all'utente `board`, tramite un override locale; lasciare il runtime separato dal virtualenv web. Dopo aver preparato runtime e modello, riavviare la propria istanza. Il microfono indica l'indisponibilità finché manca la configurazione. I pesi del modello, i virtualenv e le registrazioni non entrano nell'archivio portable.

Ogni registrazione accetta al massimo **120 secondi e 15 MiB**. Si esegue una sola trascrizione per utente Linux alla volta; una richiesta concorrente riceve un invito a riprovare. Il lavoro ha un timeout di 150 secondi. Un reverse proxy deve accettare almeno 16 MiB per `/api/audio/transcribe` e attendere almeno 180 secondi per la risposta; per gli allegati immagine resta necessario il limite di 21 MiB indicato sopra. Sono supportati WebM/Opus e MP4/AAC dei browser compatibili, oltre a WAV e Ogg verificati dal decoder.

Per ridurre l'attesa dopo Stop, il modello inizia a caricarsi mentre si registra e resta pronto per gli audio successivi. Qualità, modello e parametri non cambiano. Il processo dedicato viene chiuso dopo dieci minuti senza utilizzo, liberando la memoria; al prossimo audio riparte automaticamente. Dopo un riavvio o una pausa lunga il primo audio può richiedere più tempo, soprattutto se la registrazione è molto breve. La preparazione è facoltativa: un suo errore non interrompe la registrazione e la trascrizione può avviare il modello normalmente.

I file temporanei sono privati e rimossi alla fine della richiesta; registrazione e testo non vengono scritti nei log. L'annullamento interrompe la richiesta browser e ignora il risultato; un worker già avviato può terminare entro il proprio timeout, mantenendo occupato lo slot fino alla pulizia. Se la trascrizione supera il limite del messaggio, appare un riquadro per copiarla integralmente e la bozza esistente resta intatta. La dettatura è disabilitata in sola lettura.

## Stati e MCP opzionali

`POST /api/events` accetta JSON con `session`, `status` e `detail`, autenticato tramite header `Authorization: Bearer` con il valore di `BOARD_INGEST_TOKEN`. Gli eventi sono indipendenti dalla password del browser. Con il token già fornito all'ambiente del server e del comando da un gestore di secret:

```sh
BOARD_URL=http://127.0.0.1:18099 python3 tools/report-status.py \
  --session portable-demo --status waiting --detail "Serve una decisione"
```

Il comando legge il token soltanto dall'ambiente, non lo accetta negli argomenti e non lo stampa. `BOARD_URL` vale `http://127.0.0.1:8099` se assente; HTTP è permesso solo su loopback, altrove serve HTTPS. La richiesta ha timeout di cinque secondi e non segue redirect. Lo script usa solo la libreria standard Python. Collegarlo agli hook del proprio ambiente è una scelta esplicita: cloud-init non modifica le impostazioni globali dei CLI. Lo switch aggiunge hook soltanto al processo che riavvia, per associare conversazione, processo e stato.

L'archivio parte senza stati salvati. Un evento vecchio viene indicato come tale: non è una prova che l'agente stia ancora lavorando. La diagnostica MCP, quando attivata, misura la disponibilità osservabile dei server configurati; una porta raggiungibile da sola non verifica una chiamata a un tool MCP.

## Creare gli artefatti

Dal sorgente `portable/`:

```sh
python3 tools/build_release.py \
  --output dist/session-board-portable.tar.gz \
  --cloud-init dist/session-board-cloud-init.yaml
```

Il builder richiede solo la libreria standard Python. Stampa lo SHA256 dell'archivio e include in cloud-init lo stesso archivio in base64, senza URL privati o accesso al repository originale. Genera automaticamente anche `session-board-cloud-init.yaml.gz`, riproducibile e contenente lo stesso cloud-config. I sorgenti sono autosufficienti; l'installazione delle dipendenze richiede accesso ai repository Ubuntu e PyPI. Le dipendenze dirette sono fissate in `requirements.txt`, quelle transitive sono risolte da pip.

La whitelist comprende solo codice applicativo, asset web, documentazione e script di distribuzione. Esclude `.env`, credenziali, database, cache, virtualenv, directory git, test e configurazioni locali. I symlink vengono rifiutati. L'archivio è riproducibile a parità di sorgenti, con timestamp e proprietari normalizzati; può essere estratto in qualsiasi directory accessibile all'utente.

## Nuova VM con cloud-init

Fornire `session-board-cloud-init.yaml` come user-data a una **nuova VM Ubuntu 24.04** e configurare l'accesso SSH con la propria chiave tramite il provider. Il file generato non contiene chiavi SSH, password o token. Si può caricare anche la variante `.yaml.gz` tramite un'interfaccia che supporti file binari: cloud-init riconosce il formato gzip e lo decomprime. [Documentazione cloud-init](https://docs.cloud-init.io/en/25.1/topics/format.html#gzip-compressed-content).

Prima dell'upload controllare la dimensione stampata dal builder e il limite del provider. **Amazon EC2 consente 16 KB di user-data prima della codifica base64**; il pacchetto completo incorporato supera attualmente questo limite anche nella variante gzip. Per EC2 usare la modalità con archivio remoto descritta sotto. Il file incorporato è destinato a provider che accettano payload di dimensione maggiore. [Limite ufficiale AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/user-data.html).

Per ottenere un cloud-config piccolo, indicare l'URL HTTPS di uno storage che si controlla. `example.com` è un dominio di esempio da sostituire:

```sh
python3 tools/build_release.py \
  --output dist/session-board-portable.tar.gz \
  --cloud-init dist/session-board-cloud-init-remote.yaml \
  --archive-url https://example.com/releases/session-board-portable.tar.gz
```

Il builder **non pubblica né carica l'archivio**. Prima di creare la VM occorre rendere raggiungibile a quell'URL esattamente il file `session-board-portable.tar.gz` prodotto dallo stesso comando. Se si modifica il sorgente e si ricrea l'archivio, aggiornare insieme archivio e cloud-config. La modalità remota omette il payload base64, installa `curl`, scarica solo tramite HTTPS anche durante i redirect e interrompe il download dopo 60 secondi. Prima di estrarre verifica lo SHA256 fissato nel cloud-config: un file diverso interrompe il bootstrap. L'URL non può contenere credenziali, query string o frammenti; il generatore non configura autenticazione allo storage. Anche questa modalità richiede un collaudo completo sulla nuova VM.

Il bootstrap installa Python e tmux, crea l'utente non privilegiato `board`, estrae l'app in `/opt/session-board`, prepara il virtualenv e abilita l'unità systemd. Si interrompe al primo errore. L'unità richiede la presenza del file dell'hash e **non viene avviata dal bootstrap**.

Dopo il primo avvio, collegarsi via SSH con l'utente amministrativo della VM ed eseguire manualmente:

```sh
cloud-init status --wait
sudo -u board -H /opt/session-board/.venv/bin/python /opt/session-board/deploy/set-password.py
sudo -u board -H /opt/session-board/.venv/bin/python /opt/session-board/deploy/setup-2fa.py --username admin
sudo systemctl start session-board.service
sudo systemctl status session-board.service --no-pager
sudo -u board -H tmux new-session -d -s portable-demo
```

Dal proprio computer:

```sh
ssh -N -L 18099:127.0.0.1:8099 ubuntu@INDIRIZZO_VM
```

Aprire `http://127.0.0.1:18099` e accedere come `admin`, completando il collegamento 2FA con il codice di attivazione appena generato. Adattare `ubuntu` all'utente SSH della propria immagine. Non serve aprire la porta 8099 su Internet. Per lavorare nella sessione di prova: `sudo -u board -H tmux attach -t portable-demo` dalla VM.

L'unità usa lo stesso `/tmp` del sistema per raggiungere il socket tmux di `board`; non usare `PrivateTmp=true`, perché nasconderebbe le sessioni create al di fuori del servizio. Il codice resta di proprietà root, il processo gira come `board`, e il database scrivibile è in `/home/board/.local/state/session-board`.

## Verifica e limiti del collaudo

I test si eseguono dal checkout sorgente, perché sono esclusi dall'archivio distribuito. Richiedono `pytest` oltre alle dipendenze applicative:

```sh
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q --confcutdir=. tests
cloud-init schema --config-file dist/session-board-cloud-init.yaml
```

La verifica del formato cloud-init richiede che il comando `cloud-init` sia installato; il test relativo viene saltato dove manca. Una validazione dello schema e un avvio locale non equivalgono a un avvio completo su una VM cloud: il bootstrap su una nuova VM e i dettagli del provider vanno collaudati sul proprio ambiente prima dell'uso continuativo.

Per MCP impostare anche `MCP_SERVERS` con i nomi effettivi dei propri server, separati da virgola. `CLAUDE_BIN` seleziona la CLI, `CLAUDE_HOME` la home, `CLAUDE_CONFIG_DIR` il profilo e `MCP_PROJECT_DIR` la directory del progetto. Il modulo contiene come selezione iniziale i quattro nomi della board originale: sostituirli sul nuovo host. I controlli avviano connessioni CLI separate; non leggono le connessioni già aperte nelle sessioni. Timeout e risultati mancanti sono `Unknown`, non un guasto accertato. Il primo caricamento aggiorna automaticamente lo stato quando il controllo termina.

Il test browser opzionale nel checkout richiede `playwright` e Chrome/Chromium. Verifica il login reale, filtri, preferiti, output, invio su sessioni `cat` dedicate, abbonamenti, login guidato, selezione della conversazione, risposte concorrenti, polling MCP simulato e layout mobile. `BOARD_TEST_ARTIFACTS` sceglie dove salvare gli screenshot; i test non usano il socket tmux predefinito.
