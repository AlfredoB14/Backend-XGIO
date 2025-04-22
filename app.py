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

# Intentar cargar variables de entorno desde .env para desarrollo local
load_dotenv()

app = Flask(__name__)
# Configure CORS to allow requests from any origin
CORS(app, resources={r"/*": {"origins": "*"}})
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", "fallback-secret-key-for-development")

# Inicializar Firebase - con manejo para diferentes entornos
firebase_initialized = False  # Flag to track initialization status

# Explicitly disable Google Auth metadata server usage to prevent timeouts on Vercel
os.environ['NO_GCE_CHECK'] = 'true'
os.environ['GOOGLE_CLOUD_PROJECT'] = os.environ.get("FIREBASE_PROJECT_ID", "")

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
    # Si hay error, intentar inicializar con un método alternativo o con configuración mínima
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


#---------------------------- FIREBASE AUTHENTICATION -------------------------------
# ENDPOINTS DE AUTENTICACIÓN
#------------------------------------------------------------------------------------

@app.route("/register", methods=["POST"])
def register():
    data = request.json
    email = data.get("email")
    password = data.get("password")
    display_name = data.get("display_name")  # Nuevo campo
    
    if not email or not password or not display_name:
        return jsonify({"error": "Email, password, and display name required"}), 400
    
    try:
        # Crear usuario en Firebase Authentication
        user = auth.create_user(
            email=email,
            password=password,
            display_name=display_name  # Se almacena en el perfil del usuario
        )

        # Agregar usuario a la base de datos de Firebase Firestore
        db = firestore.client()
        user_data = {
            "uid": user.uid,
            "email": email,
            "display_name": display_name,
            "created_at": datetime.datetime.utcnow().isoformat()
        }
        db.collection("users").document(user.uid).set(user_data)

        # Crear una subcolección vacía llamada "routes"
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
        # Firebase login
        firebase_api_key = os.getenv("FIREBASE_API_KEY")
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={firebase_api_key}"
        payload = {"email": email, "password": password, "returnSecureToken": True}
        
        response = requests.post(url, json=payload)
        firebase_response = response.json()

        if "idToken" in firebase_response:
            # Crear un JWT con la información del usuario
            payload = {
                "uid": firebase_response["localId"],
                "email": firebase_response["email"],
                "display_name": firebase_response.get("displayName", ""),  # Obtener el nombre de usuario
                "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=5)  # Token expira en 5 horas
            }

            # Crear el token JWT
            token = jwt.encode(payload, app.config['SECRET_KEY'], algorithm="HS256")

            return jsonify({
                "message": "Login successful",
                "token": token,
                "display_name": firebase_response.get("displayName", ""),  # Nombre de usuario
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
        # El token está en formato "Bearer <token>", así que lo extraemos
        token = token.split()[1]

        # Verificar el JWT
        decoded_token = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
        user_uid = decoded_token["uid"]
        user_email = decoded_token["email"]
        user_display_name = decoded_token.get("display_name", "")  # Obtener el nombre de usuario

        # Puedes hacer algo con el uid, como obtener datos del usuario
        return jsonify({"message": "User data", "uid": user_uid, "email": user_email, "display_name": user_display_name})

    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token has expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid token"}), 401

#-----------------------------------------------------------
# ENDPOINTS PARA MANDAR Y OBTENER RUTAS
#-----------------------------------------------------------

@app.route("/add-route", methods=["POST"])
def add_route():
    token = request.headers.get('Authorization')

    if not token:
        return jsonify({"error": "Token missing"}), 400

    try:
        # Extraer el token del encabezado "Authorization"
        token = token.split()[1]

        # Decodificar el token JWT
        decoded_token = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
        user_uid = decoded_token["uid"]

        # Obtener los datos de la ruta del cuerpo de la solicitud
        data = request.json
        route_name = data.get("route_name")
        latitude = data.get("latitude")
        longitude = data.get("longitude")
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()  # Ya es string

        if not route_name or latitude is None or longitude is None:
            return jsonify({"error": "Route name, latitude, and longitude are required"}), 400

        # Conectar a Firestore
        db = firestore.client()
        
        # Verificar que el usuario existe
        user_ref = db.collection("users").document(user_uid)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return jsonify({"error": f"User with UID {user_uid} not found"}), 404

        # Crear o agregar a la subcolección "routes" del usuario
        route_data = {
            "route_name": route_name,
            "latitude": latitude,
            "longitude": longitude,
            "timestamp": timestamp
        }
        
        # Crear explícitamente la referencia a la subcolección y añadir el documento
        routes_ref = user_ref.collection("routes")
        
        # Usar set() con un ID generado automáticamente en lugar de add()
        import uuid
        route_id = str(uuid.uuid4())
        routes_ref.document(route_id).set(route_data)
        
        print(f"Route added with ID: {route_id}")  # Log para depuración
        
        # Responder con objetos serializables simples (strings, números, booleanos, listas, diccionarios)
        return jsonify({
            "message": "Route added successfully", 
            "route": {
                "id": route_id,
                "route_name": route_name,
                "latitude": latitude,
                "longitude": longitude,
                "timestamp": timestamp
            },
            "user_uid": user_uid
        }), 200

    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token has expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid token"}), 401
    except Exception as e:
        print(f"Error adding route: {str(e)}")  # Log del error
        return jsonify({"error": str(e)}), 500

#GET ALL ROUTES FROM USER
@app.route("/get-routes", methods=["GET"])
def get_routes():
    token = request.headers.get('Authorization')

    if not token:
        return jsonify({"error": "Token missing"}), 400

    try:
        # Extraer el token del encabezado "Authorization"
        token = token.split()[1]

        # Decodificar el token JWT
        decoded_token = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
        user_uid = decoded_token["uid"]

        # Conectar a Firestore
        db = firestore.client()
        
        # Obtener la referencia al usuario
        user_ref = db.collection("users").document(user_uid)
        
        # Verificar que el usuario existe
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return jsonify({"error": f"User with UID {user_uid} not found"}), 404

        # Obtener todas las colecciones de CurrentLocation del usuario
        current_location_ref = user_ref.collection("CurrentLocation")
        current_location_docs = current_location_ref.stream()

        locations_by_day = {}
        
        for doc in current_location_docs:
            # Obtener los datos del documento (cada documento representa un día)
            location_data = doc.to_dict()
            date = doc.id  # El ID del documento es la fecha en formato ISO
            
            # Añadir los datos a nuestro diccionario
            locations_by_day[date] = location_data

        return jsonify(locations_by_day), 200

    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token has expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid token"}), 401
    except Exception as e:
        print(f"Error getting routes: {str(e)}")
        return jsonify({"error": str(e)}), 500

#Send Current Location
@app.route("/send-current-location", methods=["POST"])
def send_current_location():
    token = request.headers.get('Authorization')

    if not token:
        return jsonify({"error": "Token missing"}), 400

    try:
        # Extraer el token del encabezado "Authorization"
        token = token.split()[1]

        # Decodificar el token JWT
        decoded_token = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
        user_uid = decoded_token["uid"]

        # Obtener los datos de la ubicación del cuerpo de la solicitud
        data = request.json
        latitude = data.get("latitude")
        longitude = data.get("longitude")
        timestamp = datetime.datetime.now(datetime.timezone.utc)

        if latitude is None or longitude is None:
            return jsonify({"error": "Latitude and longitude are required"}), 400

        # Conectar a Firestore
        db = firestore.client()

        # Verificar que el usuario existe
        user_ref = db.collection("users").document(user_uid)
        user_doc = user_ref.get()

        if not user_doc.exists:
            return jsonify({"error": f"User with UID {user_uid} not found"}), 404

        # Crear o agregar a la subcolección "CurrentLocation" del usuario
        current_date = timestamp.date().isoformat()  # Obtener la fecha actual en formato ISO
        location_data = {
            "latitude": latitude,
            "longitude": longitude,
            "timestamp": timestamp.isoformat()
        }

        # Referencia al documento del día actual en la subcolección "CurrentLocation"
        current_location_ref = user_ref.collection("CurrentLocation").document(current_date)

        # Verificar si ya existe un documento para el día actual
        current_location_doc = current_location_ref.get()

        if current_location_doc.exists:
            # Si existe, agregar la nueva ubicación a la lista existente
            current_data = current_location_doc.to_dict()
            locations = current_data.get("locations", [])
            locations.append(location_data)
            current_location_ref.update({"locations": locations})
        else:
            # Si no existe, crear un nuevo documento con la ubicación
            current_location_ref.set({"locations": [location_data]})

        return jsonify({"message": "Current location added successfully"}), 200

    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token has expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid token"}), 401
    except Exception as e:
        print(f"Error adding current location: {str(e)}")
        return jsonify({"error": str(e)}), 500

#Get Current Location
@app.route("/get-current-location", methods=["GET"])
def get_current_location():
    token = request.headers.get('Authorization')

    if not token:
        return jsonify({"error": "Token missing"}), 400

    try:
        # Extraer el token del encabezado "Authorization"
        token = token.split()[1]

        # Decodificar el token JWT
        decoded_token = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
        user_uid = decoded_token["uid"]

        # Conectar a Firestore
        db = firestore.client()

        # Obtener la referencia al usuario
        user_ref = db.collection("users").document(user_uid)

        # Verificar que el usuario existe
        user_doc = user_ref.get()

        if not user_doc.exists:
            return jsonify({"error": f"User with UID {user_uid} not found"}), 404

        # Obtener la fecha actual en formato ISO
        current_date = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

        # Obtener la ubicación actual del usuario
        current_location_ref = user_ref.collection("CurrentLocation").document(current_date)
        current_location_doc = current_location_ref.get()

        if current_location_doc.exists:
            location_data = current_location_doc.to_dict()
            return jsonify(location_data), 200
        else:
            return jsonify({"message": "No current location data found for today"}), 404

    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token has expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid token"}), 401
    except Exception as e:
        print(f"Error getting current location: {str(e)}")
        return jsonify({"error": str(e)}), 500

#get latest location
@app.route("/get-latest-location", methods=["GET"])
def get_latest_location():
    token = request.headers.get('Authorization')

    if not token:
        return jsonify({"error": "Token missing"}), 400

    try:
        # Extraer el token del encabezado "Authorization"
        token = token.split()[1]

        # Decodificar el token JWT
        decoded_token = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
        user_uid = decoded_token["uid"]

        # Conectar a Firestore
        db = firestore.client()

        # Obtener la referencia al usuario
        user_ref = db.collection("users").document(user_uid)

        # Verificar que el usuario existe
        user_doc = user_ref.get()

        if not user_doc.exists:
            return jsonify({"error": f"User with UID {user_uid} not found"}), 404

        # Obtener la ubicación actual del usuario
        current_location_ref = user_ref.collection("CurrentLocation")
        current_location_docs = current_location_ref.stream()

        latest_location = None

        for doc in current_location_docs:
            location_data = doc.to_dict()
            locations = location_data.get("locations", [])
            if locations:
                latest_location = locations[-1]  # Obtener la última ubicación registrada

        if latest_location:
            return jsonify(latest_location), 200
        else:
            return jsonify({"message": "No location data found"}), 404

    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token has expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid token"}), 401
    except Exception as e:
        print(f"Error getting latest location: {str(e)}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)