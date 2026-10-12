import hmac, os, sqlite3, time, secrets, json, urllib.request
from functools import wraps
from flask import Flask, request, session, jsonify, send_from_directory, g
from werkzeug.security import generate_password_hash as gh, check_password_hash as ch
from markupsafe import escape

BASE = os.path.dirname(os.path.abspath(__file__))
SD = os.path.join(BASE, 'static') if os.path.exists(os.path.join(BASE, 'static', 'index.html')) else BASE
app = Flask(__name__, static_folder=SD, static_url_path='/static')
app.secret_key = os.environ.get('SECRET_KEY', 'dev-change-me')
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=86400 * 365)
DB = os.path.join(os.environ.get('DATA_DIR', '.'), 'shane.db')
PLANS = {'mensuel': (10000, 30), 'annuel': (114000, 365)}
T = {'products': ['name', 'price', 'stock', 'barcode'], 'clients': ['name', 'phone'], 'expenses': ['cat', 'label', 'amount']}

def db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close(_):
    d = g.pop('db', None)
    if d: d.close()

def q(sql, a=(), one=False, commit=False):
    c = db().execute(sql, a)
    if commit:
        db().commit(); return c.lastrowid
    r = c.fetchall()
    return (r[0] if r else None) if one else r

with sqlite3.connect(DB) as c:
    c.executescript('''
    create table if not exists users(id integer primary key, shop text, phone text unique, pw text, created real, sub_until real default 0);
    create table if not exists products(id integer primary key, uid int, name text, price real, stock int default 0, barcode text default '');
    create table if not exists clients(id integer primary key, uid int, name text, phone text default '');
    create table if not exists expenses(id integer primary key, uid int, cat text, label text, amount real, created real);
    create table if not exists sales(id integer primary key, uid int, num text, code text unique, client_id int, total real, paid real, method text, created real);
    create table if not exists sale_items(id integer primary key, sale_id int, name text, price real, qty int);
    create table if not exists manual_pay(id integer primary key, uid int, plan text, ref text, status text default 'pending', created real);
    create table if not exists payments(id integer primary key, uid int, token text unique, plan text, status text default 'pending');''')

def info(u):
    now = time.time(); te = u['created'] + 7 * 86400; until = max(te, u['sub_until'])
    return dict(shop=u['shop'], phone=u['phone'], trial_end=te, sub_until=u['sub_until'], active=now < until,
                subscribed=u['sub_until'] > now, days_left=max(0, -(-int(until - now) // 86400)))

def api(gate=True):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            u = q('select * from users where id=?', (session.get('uid'),), one=True) if session.get('uid') else None
            if not u: return jsonify(error='Connexion requise'), 401
            if gate and not info(u)['active']: return jsonify(error='Abonnement requis'), 402
            g.u = u; return f(*a, **k)
        return w
    return deco

@app.route('/')
def home(): return send_from_directory(SD, 'index.html')

@app.get('/healthz')
def hz(): return 'ok'

@app.post('/api/register')
def register():
    d = request.json or {}
    shop = (d.get('shop') or '').strip(); phone = ''.join(ch_ for ch_ in d.get('phone', '') if ch_.isdigit() or ch_ == '+')
    if not shop or len(phone) < 8 or len(d.get('password', '')) < 6:
        return jsonify(error='Boutique, téléphone valide et mot de passe (6 caractères min.) requis'), 400
    if q('select 1 from users where phone=?', (phone,), one=True): return jsonify(error='Ce numéro a déjà un compte'), 409
    uid = q('insert into users(shop,phone,pw,created) values(?,?,?,?)', (shop, phone, gh(d['password']), time.time()), commit=True)
    session.permanent = True; session['uid'] = uid
    return jsonify(ok=True)

@app.post('/api/login')
def login():
    d = request.json or {}; phone = ''.join(c for c in d.get('phone', '') if c.isdigit() or c == '+')
    u = q('select * from users where phone=?', (phone,), one=True)
    if not u or not ch(u['pw'], d.get('password', '')): return jsonify(error='Téléphone ou mot de passe incorrect'), 401
    session.permanent = True; session['uid'] = u['id']; return jsonify(ok=True)

@app.post('/api/logout')
def logout(): session.clear(); return jsonify(ok=True)

@app.get('/api/me')
@api(False)
def me(): return jsonify(info(g.u))

@app.route('/api/<t>', methods=['GET', 'POST'])
@api()
def crud(t):
    if t not in T: return jsonify(error='introuvable'), 404
    uid = g.u['id']
    if request.method == 'POST':
        d = request.json or {}; cols = T[t]
        extra = ',created' if t == 'expenses' else ''; ev = (time.time(),) if t == 'expenses' else ()
        i = q(f'insert into {t}(uid,{",".join(cols)}{extra}) values(?,{",".join("?" * len(cols))}{",?" if extra else ""})',
              (uid, *[d.get(c, '') for c in cols], *ev), commit=True)
        return jsonify(id=i)
    if t == 'clients':
        r = q('select c.*, coalesce((select sum(total-paid) from sales where client_id=c.id),0) debt from clients c where uid=? order by id desc', (uid,))
    else:
        r = q(f'select * from {t} where uid=? order by id desc', (uid,))
    return jsonify([dict(x) for x in r])

@app.route('/api/<t>/<int:i>', methods=['PUT', 'DELETE'])
@api()
def crud1(t, i):
    if t not in T: return jsonify(error='introuvable'), 404
    if request.method == 'DELETE': q(f'delete from {t} where id=? and uid=?', (i, g.u['id']), commit=True)
    else:
        d = request.json or {}
        q(f'update {t} set {",".join(c + "=?" for c in T[t])} where id=? and uid=?', (*[d.get(c, '') for c in T[t]], i, g.u['id']), commit=True)
    return jsonify(ok=True)

@app.route('/api/sales', methods=['GET', 'POST'])
@api()
def sales():
    uid = g.u['id']
    if request.method == 'POST':
        d = request.json or {}; lines = []; total = 0
        for it in d.get('items', []):
            p = q('select * from products where id=? and uid=?', (it['id'], uid), one=True); n = int(it['qty'])
            if not p or n < 1: return jsonify(error='Produit invalide'), 400
            if p['stock'] < n: return jsonify(error=f"Stock insuffisant : {p['name']} ({p['stock']} restant)"), 400
            lines.append((p, n)); total += p['price'] * n
        if not lines: return jsonify(error='Panier vide'), 400
        paid = min(float(d.get('paid', total)), total); cnt = q('select count(*) c from sales where uid=?', (uid,), one=True)['c']
        num = f"{time.strftime('%Y')}-{cnt + 1:04d}"; code = secrets.token_urlsafe(8)
        sid = q('insert into sales(uid,num,code,client_id,total,paid,method,created) values(?,?,?,?,?,?,?,?)',
                (uid, num, code, d.get('client_id') or None, total, paid, d.get('method', 'Espèces'), time.time()), commit=True)
        for p, n in lines:
            q('insert into sale_items(sale_id,name,price,qty) values(?,?,?,?)', (sid, p['name'], p['price'], n))
            q('update products set stock=stock-? where id=?', (n, p['id']))
        db().commit(); return jsonify(id=sid, num=num, code=code)
    out = []
    for s in q('select s.*, c.name client, c.phone cphone from sales s left join clients c on c.id=s.client_id where s.uid=? order by s.id desc limit 200', (uid,)):
        x = dict(s); x['items'] = [dict(i) for i in q('select name,price,qty from sale_items where sale_id=?', (s['id'],))]; out.append(x)
    return jsonify(out)

@app.post('/api/sales/<int:i>/pay')
@api()
def spay(i):
    s = q('select * from sales where id=? and uid=?', (i, g.u['id']), one=True)
    if not s: return jsonify(error='introuvable'), 404
    a = max(0, float((request.json or {}).get('amount', 0)))
    q('update sales set paid=min(total,paid+?) where id=?', (a, i), commit=True); return jsonify(ok=True)

@app.get('/api/stats')
@api()
def stats():
    uid = g.u['id']; now = time.time(); day0 = now - now % 86400
    one = lambda s, *a: q(s, (uid, *a), one=True)[0] or 0
    days = [one('select sum(total) from sales where uid=? and created>=? and created<?', day0 - k * 86400, day0 - (k - 1) * 86400) for k in range(6, -1, -1)]
    return jsonify(today=days[-1], days=days, sales_today=one('select count(*) from sales where uid=? and created>=?', day0),
                   clients=one('select count(*) from clients where uid=?'), month_exp=one('select sum(amount) from expenses where uid=? and created>=?', now - 30 * 86400),
                   month_rev=one('select sum(total) from sales where uid=? and created>=?', now - 30 * 86400),
                   low=one('select count(*) from products where uid=? and stock<5'), debt=one('select sum(total-paid) from sales where uid=?'))

PD = 'https://app.paydunya.com/' + ('sandbox-api' if os.environ.get('PAYDUNYA_MODE') == 'test' else 'api') + '/v1/checkout-invoice/'
def pdh(): return {'Content-Type': 'application/json', 'PAYDUNYA-MASTER-KEY': os.environ['PAYDUNYA_MASTER_KEY'],
                   'PAYDUNYA-PRIVATE-KEY': os.environ['PAYDUNYA_PRIVATE_KEY'], 'PAYDUNYA-TOKEN': os.environ['PAYDUNYA_TOKEN']}

@app.post('/api/pay/init')
@api(False)
def pay_init():
    plan = (request.json or {}).get('plan')
    if plan not in PLANS: return jsonify(error='Offre inconnue'), 400
    if not all(os.environ.get(k) for k in ('PAYDUNYA_MASTER_KEY', 'PAYDUNYA_PRIVATE_KEY', 'PAYDUNYA_TOKEN')):
        return jsonify(error="Paiement non configuré : ajoutez vos clés PayDunya dans les variables d'environnement Render."), 503
    base = request.host_url.rstrip('/')
    body = {'invoice': {'total_amount': PLANS[plan][0], 'description': f'Abonnement SHANE+ {plan}'}, 'store': {'name': 'SHANE+'},
            'actions': {'callback_url': base + '/api/pay/ipn', 'return_url': base + '/#abonnement', 'cancel_url': base + '/#abonnement'}}
    try:
        r = json.load(urllib.request.urlopen(urllib.request.Request(PD + 'create', json.dumps(body).encode(), pdh()), timeout=20))
    except Exception as e:
        return jsonify(error='PayDunya indisponible : ' + str(e)), 502
    if r.get('response_code') != '00': return jsonify(error=r.get('response_text', 'Erreur PayDunya')), 502
    q('insert into payments(uid,token,plan) values(?,?,?)', (g.u['id'], r['token'], plan), commit=True)
    return jsonify(url=r['response_text'])

@app.post('/api/pay/ipn')
def ipn():
    tok = request.form.get('data[invoice][token]') or ((request.json or {}).get('data', {}).get('invoice', {}).get('token') if request.is_json else None)
    p = q('select * from payments where token=?', (tok,), one=True) if tok else None
    if not p or p['status'] == 'done': return 'ok'
    try:
        r = json.load(urllib.request.urlopen(urllib.request.Request(PD + 'confirm/' + tok, headers=pdh()), timeout=20))
    except Exception: return 'erreur', 502
    if r.get('status') == 'completed':
        u = q('select * from users where id=?', (p['uid'],), one=True)
        q('update users set sub_until=? where id=?', (max(time.time(), u['sub_until']) + PLANS[p['plan']][1] * 86400, u['id']))
        q("update payments set status='done' where id=?", (p['id'],), commit=True)
    return 'ok'

def grant(uid, plan):
    u = q('select * from users where id=?', (uid,), one=True)
    q('update users set sub_until=? where id=?', (max(time.time(), u['sub_until']) + PLANS[plan][1] * 86400, uid), commit=True)

PAYENV = {'wave': 'PAY_WAVE', 'om': 'PAY_OM', 'mixx': 'PAY_MIXX'}

@app.get('/api/pay/info')
@api(False)
def pay_info():
    p = q('select plan,ref,status from manual_pay where uid=? order by id desc limit 1', (g.u['id'],), one=True)
    return jsonify(methods=[m for m, k in PAYENV.items() if os.environ.get(k)], last=dict(p) if p else None)

@app.post('/api/pay/target')
@api(False)
def pay_target():
    n = os.environ.get(PAYENV.get((request.json or {}).get('m'), ''), '')
    return jsonify(number=n) if n else (jsonify(error='Moyen de paiement indisponible'), 404)

@app.post('/api/pay/manual')
@api(False)
def pay_manual():
    d = request.json or {}; plan = d.get('plan'); ref = (d.get('m', '') if d.get('m') in PAYENV else '') + ' ' + (d.get('ref') or '').strip()[:60]; ref = ref.strip()
    if plan not in PLANS or len(ref.split(' ')[-1]) < 4: return jsonify(error='Choisissez une offre et saisissez la référence du paiement (4 caractères min.)'), 400
    q('insert into manual_pay(uid,plan,ref,status,created) values(?,?,?,?,?)', (g.u['id'], plan, ref, 'pending', time.time()), commit=True)
    return jsonify(ok=True)

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    k = request.values.get('key', ''); ak = os.environ.get('ADMIN_KEY', '')
    if not ak or not hmac.compare_digest(ak, k): return 'Accès refusé', 403
    if request.method == 'POST':
        m = q('select * from manual_pay where id=?', (request.form.get('id'),), one=True)
        if m and m['status'] == 'pending':
            ok_ = request.form.get('act') == 'ok'
            if ok_: grant(m['uid'], m['plan'])
            q('update manual_pay set status=? where id=?', ('done' if ok_ else 'rejected', m['id']), commit=True)
    rows = q('select m.*,u.shop,u.phone from manual_pay m join users u on u.id=m.uid order by m.id desc limit 100')
    tr = ''.join(f"<tr><td>{time.strftime('%d/%m %H:%M', time.gmtime(r['created']))}</td><td>{escape(r['shop'])}<br>{escape(r['phone'])}</td><td>{r['plan']} ({PLANS[r['plan']][0]:,} F)</td><td><b>{escape(r['ref'])}</b></td><td>{r['status']}</td><td>" +
                 (f"<form method=post><input type=hidden name=key value='{escape(k)}'><input type=hidden name=id value={r['id']}><button name=act value=ok>Valider</button> <button name=act value=no>Refuser</button></form>" if r['status'] == 'pending' else '') + '</td></tr>' for r in rows)
    return ('<meta name=viewport content="width=device-width"><body style="font-family:sans-serif;padding:16px"><h2>Paiements à valider</h2>'
            '<p>Vérifiez la référence dans votre application Wave / Orange Money avant de valider.</p><table border=1 cellpadding=8 style="border-collapse:collapse">'
            '<tr><th>Date</th><th>Boutique</th><th>Offre</th><th>Référence</th><th>Statut</th><th></th></tr>' + tr + '</table>').replace(',', ' ')

@app.get('/verify/<code>')
def verify(code):
    s = q('select s.*,u.shop from sales s join users u on u.id=s.uid where code=?', (code,), one=True)
    body = '<h2>Facture introuvable ❌</h2>' if not s else (f"<h2>Facture authentique ✅</h2><p><b>{escape(s['shop'])}</b><br>N° {s['num']}<br>"
        f"Total : {int(s['total']):,} FCFA<br>Payé : {int(s['paid']):,} FCFA<br>{time.strftime('%d/%m/%Y', time.gmtime(s['created']))}</p>").replace(',', ' ')
    return f'<meta name=viewport content="width=device-width"><body style="font-family:sans-serif;max-width:420px;margin:40px auto;padding:20px;text-align:center">{body}<p style="color:#888">Vérifié par SHANE+</p>'

if __name__ == '__main__': app.run(debug=True)
