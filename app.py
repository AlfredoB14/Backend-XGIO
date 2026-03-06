from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import auth, credentials
import requests
import os 
import datetime
import jwt
from firebase_admin import firestore
from flask_cors import CORS
from dotenv import load_dotenv
import json
import math  # ← NUEVO: para cálculo de distancias Haversine

# Intentar cargar variables de entorno desde .env para desarrollo local
load_dotenv()

app = Flask(__name__)
# Configure CORS to allow requests from any origin
CORS(app, resources={r"/*": {"origins": "*"}})
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", "fallback-secret-key-for-development")

# Inicializar Firebase - con manejo para diferentes entornos
firebase_initialized = False  # Flag to track initialization status
try:
    # Primero intentar con archivo JSON de credenciales para desarrollo local o si existe en Vercel
    if os.path.exists('XGIO_Credentials.json'):
        cred = credentials.Certificate('XGIO_Credentials.json')
    # Si no hay archivo, intentar con variables de entorno
    else:
        cred_dict = {
            "type": os.environ.get("FIREBASE_TYPE"),
            "project_id": os.environ.get("FIREBASE_PROJECT_ID"),
            "private_key_id": os.environ.get("FIREBASE_PRIVATE_KEY_ID"),
            "private_key": os.environ.get("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n"),
            "client_email": os.environ.get("FIREBASE_CLIENT_EMAIL"),
            "client_id": os.environ.get("FIREBASE_CLIENT_ID"),
            "auth_uri": os.environ.get("FIREBASE_AUTH_URI"),
            "token_uri": os.environ.get("FIREBASE_TOKEN_URI"),
            "auth_provider_x509_cert_url": os.environ.get("FIREBASE_AUTH_PROVIDER_X509_CERT_URL"),
            "client_x509_cert_url": os.environ.get("FIREBASE_CLIENT_X509_CERT_URL"),
            "universe_domain": os.environ.get("FIREBASE_UNIVERSE_DOMAIN")
        }
        cred = credentials.Certificate(cred_dict)

    if not firebase_admin._apps:  # Evitar inicialización múltiple
        firebase_admin.initialize_app(cred)
        firebase_initialized = True
        print("Firebase initialized successfully")
except Exception as e:
    print(f"Error initializing Firebase: {str(e)}")
    try:
        if not firebase_admin._apps:
            firebase_admin.initialize_app()
            firebase_initialized = True
            print("Firebase initialized with default configuration")
    except Exception as e2:
        print(f"Failed second attempt to initialize Firebase: {str(e2)}")

# Helper function to check Firebase initialization before accessing services
def ensure_firebase_initialized():
    if not firebase_initialized and not firebase_admin._apps:
        raise Exception("The default Firebase app does not exist. Firebase initialization failed.")


# ═══════════════════════════════════════════════════════════════
#  UTILIDADES PARA POLYLINE
# ═══════════════════════════════════════════════════════════════

def encode_polyline(coordinates: list) -> str:
    """
    Codifica una lista de puntos {"lat": float, "lng": float} al formato
    Google Encoded Polyline Algorithm.
    Referencia: https://developers.google.com/maps/documentation/utilities/polylinealgorithm

    Uso en Google Maps JS API:
        const polyline = new google.maps.Polyline({
            path: google.maps.geometry.encoding.decodePath(encoded_polyline),
            strokeColor: "#4285F4",
            strokeWeight: 4,
        });
        polyline.setMap(map);
    """
    def _encode_value(value: float) -> str:
        value = int(round(value * 1e5))
        value = value << 1
        if value < 0:
            value = ~value
        chunks = []
        while value >= 0x20:
            chunks.append(chr((0x20 | (value & 0x1F)) + 63))
            value >>= 5
        chunks.append(chr(value + 63))
        return "".join(chunks)

    result = []
    prev_lat = 0
    prev_lng = 0

    for point in coordinates:
        lat = point["lat"]
        lng = point["lng"]
        result.append(_encode_value(lat - prev_lat))
        result.append(_encode_value(lng - prev_lng))
        prev_lat = lat
        prev_lng = lng

    return "".join(result)


def calculate_total_distance_km(coordinates: list) -> float:
    """
    Calcula la distancia total recorrida en kilómetros usando Haversine.
    Recibe una lista de {"lat": float, "lng": float}.
    """
    R = 6371  # Radio de la Tierra en km
    total = 0.0

    for i in range(1, len(coordinates)):
        lat1 = math.radians(coordinates[i - 1]["lat"])
        lat2 = math.radians(coordinates[i]["lat"])
        dlat = lat2 - lat1
        dlng = math.radians(coordinates[i]["lng"] - coordinates[i - 1]["lng"])
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
        total += R * 2 * math.asin(math.sqrt(a))

    return round(total, 4)


# ═══════════════════════════════════════════════════════════════
#  AUTENTICACIÓN
# ═══════════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def home():
    return jsonify({"message": "Welcome to the XGIO API"}), 200

@app.route("/register", methods=["POST"])
def register():
    data = request.json
    email = data.get("email")
    password = data.get("password")
    display_name = data.get("display_name")

    if not email or not password or not display_name:
        return jsonify({"error": "Email, password, and display name required"}), 400

    try:
        user = auth.create_user(email=email, password=password, display_name=display_name)

        db = firestore.client()
        user_data = {
            "uid": user.uid,
            "email": email,
            "display_name": display_name,
            "cane_id": None,
            "created_at": datetime.datetime.utcnow().isoformat()
        }
        db.collection("users").document(user.uid).set(user_data)

        db.collection("users").document(user.uid).collection("routes").document("placeholder").set({})
        db.collection("users").document(user.uid).collection("routes").document("placeholder").delete()

        return jsonify({"uid": user.uid, "message": "User created successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/login", methods=["POST"])
def login():
    data = request.json
    email = data.get("email")
    password = data.get("password")

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    try:
        firebase_api_key = os.getenv("FIREBASE_API_KEY")
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={firebase_api_key}"
        payload = {"email": email, "password": password, "returnSecureToken": True}

        response = requests.post(url, json=payload)
        firebase_response = response.json()

        if "idToken" in firebase_response:
            payload = {
                "uid": firebase_response["localId"],
                "email": firebase_response["email"],
                "display_name": firebase_response.get("displayName", ""),
                "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=5)
            }
            token = jwt.encode(payload, app.config['SECRET_KEY'], algorithm="HS256")
            return jsonify({
                "message": "Login successful",
                "token": token,
                "display_name": firebase_response.get("displayName", ""),
            })
        else:
            return jsonify({"error": firebase_response.get("error", {}).get("message", "Authentication failed")}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/user-data", methods=["GET"])
def user_data():
    token = request.headers.get('Authorization')
    if not token:
        return jsonify({"error": "Token missing"}), 400

    try:
        token = token.split()[1]
        decoded_token = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
        return jsonify({
            "message": "User data",
            "uid": decoded_token["uid"],
            "email": decoded_token["email"],
            "display_name": decoded_token.get("display_name", "")
        })
    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token has expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid token"}), 401


# ═══════════════════════════════════════════════════════════════
#  RUTAS
# ═══════════════════════════════════════════════════════════════

@app.route("/add-route", methods=["POST"])
def add_route():
    token = request.headers.get('Authorization')
    if not token:
        return jsonify({"error": "Token missing"}), 400

    try:
        token = token.split()[1]
        decoded_token = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
        user_uid = decoded_token["uid"]

        data = request.json
        route_name = data.get("route_name")
        latitude = data.get("latitude")
        longitude = data.get("longitude")
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()

        if not route_name or latitude is None or longitude is None:
            return jsonify({"error": "Route name, latitude, and longitude are required"}), 400

        db = firestore.client()
        user_ref = db.collection("users").document(user_uid)

        if not user_ref.get().exists:
            return jsonify({"error": f"User with UID {user_uid} not found"}), 404

        route_data = {
            "route_name": route_name,
            "latitude": latitude,
            "longitude": longitude,
            "timestamp": timestamp
        }

        import uuid
        route_id = str(uuid.uuid4())
        user_ref.collection("routes").document(route_id).set(route_data)

        return jsonify({
            "message": "Route added successfully",
            "route": {"id": route_id, **route_data},
            "user_uid": user_uid
        }), 200

    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token has expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid token"}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/get-routes", methods=["GET"])
def get_routes():
    token = request.headers.get('Authorization')
    if not token:
        return jsonify({"error": "Token missing"}), 400

    try:
        token = token.split()[1]
        decoded_token = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
        user_uid = decoded_token["uid"]

        db = firestore.client()
        user_ref = db.collection("users").document(user_uid)

        if not user_ref.get().exists:
            return jsonify({"error": f"User with UID {user_uid} not found"}), 404

        locations_by_day = {}
        for doc in user_ref.collection("CurrentLocation").stream():
            locations_by_day[doc.id] = doc.to_dict()

        return jsonify(locations_by_day), 200

    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token has expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid token"}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
#  UBICACIÓN EN TIEMPO REAL
# ═══════════════════════════════════════════════════════════════

@app.route("/send-current-location", methods=["POST"])
def send_current_location():
    try:
        data = request.json
        latitude = data.get("latitude")
        longitude = data.get("longitude")
        cane_id = data.get("cane_id")
        timestamp = datetime.datetime.now(datetime.timezone.utc)

        if latitude is None or longitude is None or cane_id is None:
            return jsonify({"error": "Latitude, longitude, and cane_id are required"}), 400

        db = firestore.client()

        matching_users = list(db.collection("users").where("cane_id", "==", cane_id).stream())
        if not matching_users:
            return jsonify({"error": f"No user found with cane_id: {cane_id}"}), 404

        user_uid = matching_users[0].id
        current_date = timestamp.date().isoformat()

        location_data = {
            "latitude": latitude,
            "longitude": longitude,
            "timestamp": timestamp.isoformat()
        }

        user_ref = db.collection("users").document(user_uid)
        current_location_ref = user_ref.collection("CurrentLocation").document(current_date)
        current_location_doc = current_location_ref.get()

        if current_location_doc.exists:
            locations = current_location_doc.to_dict().get("locations", [])
            locations.append(location_data)
            current_location_ref.update({"locations": locations})
        else:
            current_location_ref.set({"locations": [location_data]})

        return jsonify({"message": "Current location added successfully", "user_uid": user_uid}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/get-current-location", methods=["GET"])
def get_current_location():
    token = request.headers.get('Authorization')
    if not token:
        return jsonify({"error": "Token missing"}), 400

    try:
        token = token.split()[1]
        decoded_token = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
        user_uid = decoded_token["uid"]

        db = firestore.client()
        user_ref = db.collection("users").document(user_uid)

        if not user_ref.get().exists:
            return jsonify({"error": f"User with UID {user_uid} not found"}), 404

        current_date = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        current_location_doc = user_ref.collection("CurrentLocation").document(current_date).get()

        if current_location_doc.exists:
            return jsonify(current_location_doc.to_dict()), 200
        else:
            return jsonify({"message": "No current location data found for today"}), 404

    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token has expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid token"}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/get-latest-location", methods=["GET"])
def get_latest_location():
    token = request.headers.get('Authorization')
    if not token:
        return jsonify({"error": "Token missing"}), 400

    try:
        token = token.split()[1]
        decoded_token = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
        user_uid = decoded_token["uid"]

        db = firestore.client()
        user_ref = db.collection("users").document(user_uid)

        if not user_ref.get().exists:
            return jsonify({"error": f"User with UID {user_uid} not found"}), 404

        latest_location = None
        for doc in user_ref.collection("CurrentLocation").stream():
            locations = doc.to_dict().get("locations", [])
            if locations:
                latest_location = locations[-1]

        if latest_location:
            return jsonify(latest_location), 200
        else:
            return jsonify({"message": "No location data found"}), 404

    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token has expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid token"}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
#  NUEVO: POLYLINE / RECORRIDO
# ═══════════════════════════════════════════════════════════════

@app.route("/get-polyline", methods=["GET"])
def get_polyline():
    """
    Genera una polyline codificada a partir de los puntos GPS del día indicado.

    Query params:
      - date (opcional): YYYY-MM-DD. Sin este parámetro usa el día de hoy.

    Headers:
      - Authorization: Bearer <JWT>

    Respuesta:
    {
        "date": "2025-03-06",
        "total_points": 42,
        "total_distance_km": 1.2345,
        "encoded_polyline": "abcde~fghij...",
        "coordinates": [
            {"lat": 19.4326, "lng": -99.1332, "timestamp": "2025-03-06T10:00:00+00:00"},
            ...
        ]
    }
    """
    token = request.headers.get('Authorization')
    if not token:
        return jsonify({"error": "Token missing"}), 400

    try:
        token = token.split()[1]
        decoded_token = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
        user_uid = decoded_token["uid"]

        # Fecha objetivo: parámetro opcional, por defecto hoy
        target_date = request.args.get(
            "date",
            datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        )

        db = firestore.client()
        user_ref = db.collection("users").document(user_uid)

        if not user_ref.get().exists:
            return jsonify({"error": f"User with UID {user_uid} not found"}), 404

        location_doc = user_ref.collection("CurrentLocation").document(target_date).get()

        if not location_doc.exists:
            return jsonify({"message": f"No location data found for {target_date}"}), 404

        raw_locations = location_doc.to_dict().get("locations", [])

        if not raw_locations:
            return jsonify({"message": "No location points found", "date": target_date}), 404

        # Paso 1: Ordenar cronológicamente por timestamp
        sorted_locations = sorted(raw_locations, key=lambda x: x.get("timestamp", ""))

        # Paso 2: Construir lista de coordenadas limpia
        coordinates = [
            {
                "lat": float(loc["latitude"]),
                "lng": float(loc["longitude"]),
                "timestamp": loc.get("timestamp", "")
            }
            for loc in sorted_locations
            if loc.get("latitude") is not None and loc.get("longitude") is not None
        ]

        if not coordinates:
            return jsonify({"error": "No valid coordinates found"}), 400

        # Paso 3: Codificar polyline (solo lat/lng, sin timestamp)
        coords_for_encoding = [{"lat": c["lat"], "lng": c["lng"]} for c in coordinates]
        encoded = encode_polyline(coords_for_encoding)

        # Paso 4: Calcular distancia total
        total_distance = calculate_total_distance_km(coords_for_encoding)

        return jsonify({
            "date": target_date,
            "total_points": len(coordinates),
            "total_distance_km": total_distance,
            "encoded_polyline": encoded,
            "coordinates": coordinates
        }), 200

    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token has expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid token"}), 401
    except Exception as e:
        print(f"Error generating polyline: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/get-polyline-range", methods=["GET"])
def get_polyline_range():
    """
    Genera una polyline que abarca varios días consecutivos.

    Query params:
      - start_date: YYYY-MM-DD  (requerido)
      - end_date:   YYYY-MM-DD  (requerido)

    Headers:
      - Authorization: Bearer <JWT>
    """
    token = request.headers.get('Authorization')
    if not token:
        return jsonify({"error": "Token missing"}), 400

    try:
        token = token.split()[1]
        decoded_token = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
        user_uid = decoded_token["uid"]

        start_date_str = request.args.get("start_date")
        end_date_str = request.args.get("end_date")

        if not start_date_str or not end_date_str:
            return jsonify({"error": "start_date and end_date are required (YYYY-MM-DD)"}), 400

        start_date = datetime.date.fromisoformat(start_date_str)
        end_date = datetime.date.fromisoformat(end_date_str)

        if start_date > end_date:
            return jsonify({"error": "start_date must be before end_date"}), 400

        db = firestore.client()
        user_ref = db.collection("users").document(user_uid)

        if not user_ref.get().exists:
            return jsonify({"error": f"User with UID {user_uid} not found"}), 404

        # Recolectar todos los puntos del rango
        all_raw_locations = []
        current = start_date
        while current <= end_date:
            doc = user_ref.collection("CurrentLocation").document(current.isoformat()).get()
            if doc.exists:
                all_raw_locations.extend(doc.to_dict().get("locations", []))
            current += datetime.timedelta(days=1)

        if not all_raw_locations:
            return jsonify({"message": f"No location data found between {start_date_str} and {end_date_str}"}), 404

        sorted_locations = sorted(all_raw_locations, key=lambda x: x.get("timestamp", ""))

        coordinates = [
            {
                "lat": float(loc["latitude"]),
                "lng": float(loc["longitude"]),
                "timestamp": loc.get("timestamp", "")
            }
            for loc in sorted_locations
            if loc.get("latitude") is not None and loc.get("longitude") is not None
        ]

        if not coordinates:
            return jsonify({"error": "No valid coordinates found"}), 400

        coords_for_encoding = [{"lat": c["lat"], "lng": c["lng"]} for c in coordinates]
        encoded = encode_polyline(coords_for_encoding)
        total_distance = calculate_total_distance_km(coords_for_encoding)

        return jsonify({
            "start_date": start_date_str,
            "end_date": end_date_str,
            "total_points": len(coordinates),
            "total_distance_km": total_distance,
            "encoded_polyline": encoded,
            "coordinates": coordinates
        }), 200

    except ValueError as ve:
        return jsonify({"error": f"Invalid date format: {str(ve)}"}), 400
    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token has expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid token"}), 401
    except Exception as e:
        print(f"Error generating polyline range: {str(e)}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)