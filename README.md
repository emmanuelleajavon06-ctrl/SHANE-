# SHANE+ — Déploiement sur Render
1. Mettez ce dossier sur GitHub. 2. Render > New > Web Service > repo. Build: `pip install -r requirements.txt` — Start: `gunicorn app:app`
3. Variables d'environnement : SECRET_KEY, PAYDUNYA_MASTER_KEY, PAYDUNYA_PRIVATE_KEY, PAYDUNYA_TOKEN (+ PAYDUNYA_MODE=test pour tester).
4. Données : ajoutez un Disk Render monté sur /data et définissez DATA_DIR=/data, sinon les comptes sont perdus à chaque redéploiement.
Test local : pip install -r requirements.txt && python app.py

5. Paiement manuel (sans RCCM) : variables PAY_WAVE, PAY_OM (vos numéros) et ADMIN_KEY (mot de passe). Validez les paiements sur /admin?key=VOTRE_ADMIN_KEY
