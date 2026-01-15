# 🚀 LeseAssistent Deployment auf Coolify (Hetzner)

## Voraussetzungen

- GitHub Account
- Coolify läuft auf deinem Hetzner Server
- Domain (z.B. `lesen.wilski.tech`) bereits auf Hetzner IP zeigend

---

## Schritt 1: Projektstruktur vorbereiten

Dein Projektordner sollte so aussehen:

```
leseassistent/
├── app.py
├── Dockerfile          ← NEU (von mir erstellt)
├── requirements.txt
├── .gitignore
├── README.md
├── static/
│   ├── d.png           (Deutsche Flagge)
│   ├── t.png           (Türkische Flagge)
│   └── b.png           (Bulgarische Flagge)
└── templates/
    ├── index.html
    ├── aufgaben.html
    ├── nachsprechen.html
    ├── student.html
    └── teacher.html
```

**Wichtig:** Kopiere das Dockerfile in deinen Projektordner!

---

## Schritt 2: GitHub Repository erstellen

### Option A: Über GitHub Website
1. Gehe zu https://github.com/new
2. Repository Name: `leseassistent`
3. Private oder Public (egal für Coolify)
4. **NICHT** "Add README" anklicken
5. Create Repository

### Option B: Dann lokal pushen

```powershell
cd "D:\KI Projekte\leseassistent 2"

# Git initialisieren (falls noch nicht geschehen)
git init

# Alle Dateien hinzufügen
git add .

# Commit erstellen
git commit -m "Initial commit: LeseAssistent"

# Remote hinzufügen (ersetze USERNAME mit deinem GitHub-Namen)
git remote add origin https://github.com/USERNAME/leseassistent.git

# Pushen
git branch -M main
git push -u origin main
```

---

## Schritt 3: In Coolify einrichten

### 3.1 Neues Projekt anlegen
1. Öffne Coolify (https://deine-coolify-url)
2. Klicke auf **"+ Add New Resource"** oder **"New Project"**
3. Wähle **"Application"**

### 3.2 GitHub verbinden
1. Source: **GitHub** (Public oder Private Repository)
2. Repository URL: `https://github.com/USERNAME/leseassistent`
3. Branch: `main`

### 3.3 Build-Einstellungen
| Einstellung | Wert |
|-------------|------|
| Build Pack | **Dockerfile** |
| Dockerfile Location | `Dockerfile` (Standard) |
| Port | `5000` |

### 3.4 Domain konfigurieren
1. Gehe zu **"Domains"** oder **"Settings"**
2. Füge hinzu: `lesen.wilski.tech` (oder deine gewünschte Domain)
3. **SSL/HTTPS** aktivieren (Let's Encrypt)

### 3.5 Umgebungsvariablen (optional)
| Variable | Wert | Beschreibung |
|----------|------|--------------|
| `SECRET_KEY` | `dein-geheimer-schluessel-hier` | Für Flask Sessions |
| `ASYNC_MODE` | `gevent` | WebSocket Mode |

**Hinweis:** Die API-Keys (ElevenLabs, OpenAI, etc.) werden NICHT als Env-Variablen gesetzt - sie kommen von den Nutzern (BYOK)!

### 3.6 Deploy starten
1. Klicke **"Deploy"** oder **"Save & Deploy"**
2. Warte 2-5 Minuten
3. Prüfe die Logs auf Fehler

---

## Schritt 4: DNS einrichten (falls noch nicht geschehen)

In deinem Domain-Provider (z.B. Cloudflare, IONOS):

| Typ | Name | Wert | TTL |
|-----|------|------|-----|
| A | `lesen` | `<Hetzner-IP>` | Auto |

Oder wenn du Wildcard hast:
| Typ | Name | Wert |
|-----|------|------|
| A | `*` | `<Hetzner-IP>` |

---

## Schritt 5: Testen

1. Öffne `https://lesen.wilski.tech` im Browser
2. Du solltest die LeseAssistent Startseite sehen
3. Teste den Lehrer-Modus mit deinen API-Keys

---

## 🔧 Troubleshooting

### WebSocket-Fehler
Wenn Schüler nicht beitreten können:
- Prüfe ob Coolify WebSocket-Proxy aktiviert ist
- In manchen Setups muss `Sticky Sessions` aktiviert sein

### 502 Bad Gateway
- Warte 30 Sekunden und lade neu (Cold Start)
- Prüfe Logs in Coolify

### Bilder fehlen (Flaggen)
- Stelle sicher, dass `static/d.png`, `t.png`, `b.png` vorhanden sind
- Oder entferne die Flaggen-Referenzen aus den Templates

---

## 📝 Updates deployen

Nach Änderungen am Code:

```powershell
git add .
git commit -m "Beschreibung der Änderung"
git push
```

Coolify deployed automatisch (wenn Auto-Deploy aktiviert ist) oder klicke manuell auf **"Redeploy"**.

---

## ✅ Checkliste

- [ ] Dockerfile im Projektordner
- [ ] GitHub Repository erstellt
- [ ] Code gepusht
- [ ] Coolify Application angelegt
- [ ] Domain konfiguriert
- [ ] SSL aktiviert
- [ ] Deploy erfolgreich
- [ ] Website erreichbar
- [ ] WebSocket funktioniert (Lehrer/Schüler Session)
