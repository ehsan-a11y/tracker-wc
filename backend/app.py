from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import sqlite3
import os
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Vercel has a read-only filesystem except /tmp (data resets on cold start)
IS_VERCEL = bool(os.environ.get('VERCEL'))
DB            = '/tmp/shipments.db'              if IS_VERCEL else os.path.join(BASE_DIR, 'shipments.db')
UPLOAD_FOLDER = '/tmp/uploads'                   if IS_VERCEL else os.path.join(BASE_DIR, 'uploads')
TEMPLATE_DIR  = os.path.join(BASE_DIR, 'templates')

app = Flask(__name__, template_folder=TEMPLATE_DIR)
CORS(app)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS shipments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ship_date TEXT,
        awb TEXT UNIQUE,
        shipping_cost REAL,
        status TEXT,
        invoice_file TEXT,
        awb_file TEXT
    )
    """)
    conn.commit()
    conn.close()


init_db()


def save_file(field_name):
    if field_name in request.files:
        f = request.files[field_name]
        if f and f.filename:
            filename = secure_filename(f.filename)
            path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            f.save(path)
            return filename
    return None


@app.route('/api/shipments', methods=['GET'])
def get_shipments():
    conn = get_db()
    query = "SELECT * FROM shipments WHERE 1=1"
    params = []

    if request.args.get('date'):
        query += " AND ship_date=?"
        params.append(request.args.get('date'))
    if request.args.get('awb'):
        query += " AND awb LIKE ?"
        params.append(f"%{request.args.get('awb')}%")
    if request.args.get('status'):
        query += " AND status=?"
        params.append(request.args.get('status'))
    if request.args.get('month'):
        # month format: "Mar 2026" — ship_date format: "26, Mar, 2026"
        parts = request.args.get('month').split()
        if len(parts) == 2:
            query += " AND ship_date LIKE ?"
            params.append(f"%, {parts[0]}, {parts[1]}")

    query += " ORDER BY id DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/shipments', methods=['POST'])
def add_shipment():
    data = request.form
    awb = data.get('awb', '').strip()
    if not awb:
        return jsonify({'error': 'AWB No is required'}), 400

    invoice_filename = save_file('invoice_file') or ''
    awb_filename = save_file('awb_file') or ''

    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO shipments (ship_date, awb, shipping_cost, status, invoice_file, awb_file) VALUES (?,?,?,?,?,?)",
            (data.get('ship_date'), awb, float(data.get('shipping_cost') or 0),
             data.get('status', 'Transit'), invoice_filename, awb_filename)
        )
        conn.commit()
        conn.close()
        return jsonify({'message': 'Added successfully'})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'AWB already exists'}), 409


@app.route('/api/shipments/<int:sid>', methods=['PUT'])
def update_shipment(sid):
    data = request.form
    invoice_filename = save_file('invoice_file') or data.get('invoice_file_current', '')
    awb_filename = save_file('awb_file') or data.get('awb_file_current', '')

    try:
        conn = get_db()
        conn.execute(
            "UPDATE shipments SET ship_date=?, awb=?, shipping_cost=?, status=?, invoice_file=?, awb_file=? WHERE id=?",
            (data.get('ship_date'), data.get('awb', '').strip(),
             float(data.get('shipping_cost') or 0), data.get('status', 'Transit'),
             invoice_filename, awb_filename, sid)
        )
        conn.commit()
        conn.close()
        return jsonify({'message': 'Updated successfully'})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'AWB already exists'}), 409


@app.route('/api/shipments/<int:sid>', methods=['DELETE'])
def delete_shipment(sid):
    conn = get_db()
    conn.execute("DELETE FROM shipments WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Deleted'})


@app.route('/api/dashboard')
def dashboard():
    conn = get_db()

    def count(where=''):
        return conn.execute(f"SELECT COUNT(*) FROM shipments {where}").fetchone()[0]

    data = {
        'total': count(),
        'transit': count("WHERE status='Transit'"),
        'delivered': count("WHERE status='Delivered'"),
        'returned': count("WHERE status='Returned'")
    }
    conn.close()
    return jsonify(data)


@app.route('/api/monthly')
def monthly():
    conn = get_db()
    rows = conn.execute("""
        SELECT ship_date,
               SUM(CASE WHEN status='Transit'   THEN 1 ELSE 0 END) AS transit,
               SUM(CASE WHEN status='Delivered' THEN 1 ELSE 0 END) AS delivered,
               SUM(CASE WHEN status='Returned'  THEN 1 ELSE 0 END) AS returned
        FROM shipments
        GROUP BY ship_date
        ORDER BY ship_date
    """).fetchall()
    conn.close()

    # Aggregate by month label (e.g. "Jan 2025")
    from collections import OrderedDict
    months_map = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
                  'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
    buckets = OrderedDict()
    for r in rows:
        parts = str(r['ship_date']).replace(',','').split()
        if len(parts) >= 3:
            try:
                label = f"{parts[1]} {parts[2]}"
                sort_key = (int(parts[2]), months_map.get(parts[1], 0))
                if label not in buckets:
                    buckets[label] = {'month': label, '_sort': sort_key, 'transit': 0, 'delivered': 0, 'returned': 0}
                buckets[label]['transit']   += r['transit']
                buckets[label]['delivered'] += r['delivered']
                buckets[label]['returned']  += r['returned']
            except Exception:
                pass

    result = sorted(buckets.values(), key=lambda x: x['_sort'])
    for item in result:
        del item['_sort']
    return jsonify(result)


@app.route('/uploads/<filename>')
def serve_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route('/')
def index():
    return send_from_directory(TEMPLATE_DIR, 'index.html')


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
