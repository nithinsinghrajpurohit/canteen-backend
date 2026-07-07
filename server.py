from fastapi import FastAPI, APIRouter, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
import random
from typing import List, Optional
import uuid
from datetime import datetime, timezone
import bcrypt
import requests
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
try:
    import warnings
    # Suppress deprecation warning from google.generativeai
    warnings.filterwarnings("ignore", category=FutureWarning)
    import google.generativeai as genai
except ImportError:
    genai = None
    print("⚠️  WARNING: google-generativeai library not found. AI features will be disabled.")
    print("👉  Install it using: pip install google-generativeai")


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ.get('MONGO_URL', 'mongodb://localhost:27017')
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ.get('DB_NAME', 'smartcanteen')]

# Configure Gemini AI
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyAUZ52Jwq5Qt9_BQ84-MQKcPChIsktI3eU")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "999961982575-ve0j0bk40ge7a8n1mq1qdvsck03fi10d.apps.googleusercontent.com")
if genai:
    genai.configure(api_key=GEMINI_API_KEY)

# Create the main app
app = FastAPI(title="Smart Canteen API", version="1.0.0")

# Create router with /api prefix
api_router = APIRouter(prefix="/api")

# ==================== Pydantic Models ====================

class FacultyLoginRequest(BaseModel):
    email: str
    password: str

class StudentLoginRequest(BaseModel):
    roll_number: str
    dob: str

# ✅ UPDATED: Added phone field
class StudentSignupRequest(BaseModel):
    name: str
    roll_number: str
    branch: str
    year: int
    section: Optional[str] = None
    phone: Optional[str] = None  # ← NEW: Phone number (optional)
    dob: str
    email: Optional[str] = None

class FacultySignupRequest(BaseModel):
    name: str
    email: str
    phone: Optional[str] = None
    password: str

class LoginResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str
    user_type: str
    name: str
    roll_number: Optional[str] = None
    email: Optional[str] = None

class MenuItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    description: str
    price: float
    category: str
    image_url: str
    available: bool = True

class AddToCartRequest(BaseModel):
    user_id: str
    menu_item_id: str
    quantity: int = 1

class CartResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str
    items: List[dict]
    total_amount: float

class PlaceOrderRequest(BaseModel):
    user_id: str

class OrderResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    order_id: str
    user_id: str
    items: List[dict]
    total_amount: float
    status: str
    created_at: str
    user_name: Optional[str] = None
    user_details: Optional[str] = None
    user_phone: Optional[str] = None

class ChatRequest(BaseModel):
    message: str
    weather: Optional[str] = None

class GoogleLoginRequest(BaseModel):
    credential: str

class GoogleStudentSignupRequest(BaseModel):
    credential: str
    roll_number: str
    phone: str
    branch: str
    year: int
    section: Optional[str] = None

class UpdateOrderStatusRequest(BaseModel):
    order_id: str
    status: str

# ==================== ROUTES ====================

@api_router.get("/")
async def root():
    return {"message": "Smart Canteen API - Ready! 🚀"}

# STUDENT LOGIN (Roll Number + DOB)
@api_router.post("/auth/login-student", response_model=LoginResponse)
async def login_student(request: StudentLoginRequest):
    roll = request.roll_number.upper()
    student = await db.students.find_one({"roll_number": roll})
    
    if not student:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    # Verify DOB
    if student.get("dob") != request.dob:
        raise HTTPException(status_code=401, detail="Invalid Date of Birth")
    
    return LoginResponse(
        user_id=student["user_id"],
        user_type="student",
        name=student["name"],
        roll_number=student["roll_number"]
    )

# FACULTY LOGIN
@api_router.post("/auth/login-faculty", response_model=LoginResponse)
async def login_faculty(request: FacultyLoginRequest):
    faculty = await db.faculty.find_one({"email": request.email}, {"_id": 0})
    
    if not faculty:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    if not bcrypt.checkpw(request.password.encode('utf-8'), faculty["password"].encode('utf-8')):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    print(f"✅ Faculty logged in: {faculty['name']} ({request.email})")
    return LoginResponse(
        user_id=faculty["user_id"],
        user_type="faculty",
        name=faculty["name"],
        email=faculty["email"]
    )

# ✅ UPDATED: Student Signup with PHONE
@api_router.post("/auth/signup-student")
async def signup_student(request: StudentSignupRequest):
    roll_number = request.roll_number.upper()
    
    # Check duplicate
    existing = await db.students.find_one({"roll_number": roll_number})
    if existing:
        raise HTTPException(status_code=409, detail="Student with this roll number already exists")
    
    # Check duplicate phone
    if request.phone:
        if await db.students.find_one({"phone": request.phone}):
            raise HTTPException(status_code=409, detail="Phone number already registered by another student")
    
    student_data = {
        "user_id": str(uuid.uuid4()),
        "name": request.name,
        "roll_number": roll_number,
        "branch": request.branch,
        "year": request.year,
        "section": request.section,
        "phone": request.phone,  # ← NEW: Save phone number
        "dob": request.dob,
        "email": request.email,
        "user_type": "student",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.students.insert_one(student_data)
    print(f"✅ NEW Student: {request.name} ({roll_number}) - Phone: {request.phone or 'N/A'}")
    return {"message": "Signup successful! Please login."}

# FACULTY SIGNUP
@api_router.post("/auth/signup-faculty")
async def signup_faculty(request: FacultySignupRequest):
    # Check duplicate
    existing = await db.faculty.find_one({"email": request.email})
    if existing:
        raise HTTPException(status_code=409, detail="Faculty with this email already exists")
    
    hashed_password = bcrypt.hashpw(request.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    faculty_data = {
        "user_id": str(uuid.uuid4()),
        "name": request.name,
        "email": request.email,
        "phone": request.phone,
        "password": hashed_password,
        "user_type": "faculty",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.faculty.insert_one(faculty_data)
    print(f"✅ NEW Faculty: {request.name} ({request.email})")
    return {"message": "Faculty account created successfully!"}

# GOOGLE LOGIN
@api_router.post("/auth/google-login", response_model=LoginResponse)
async def google_login(request: GoogleLoginRequest):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Google Client ID is not configured on the server.")

    try:
        # Verify the ID token
        idinfo = id_token.verify_oauth2_token(
            request.credential, google_requests.Request(), GOOGLE_CLIENT_ID
        )

        email = idinfo.get("email")

        if not email:
            raise HTTPException(status_code=400, detail="Email not found in Google token.")

        # 1. Check Faculty
        faculty = await db.faculty.find_one({"email": email}, {"_id": 0})
        if faculty:
            print(f"✅ Google Login (Faculty): {faculty['name']} ({email})")
            return LoginResponse(
                user_id=faculty["user_id"],
                user_type="faculty",
                name=faculty["name"],
                email=faculty["email"]
            )
            
        # 2. Check Student
        student = await db.students.find_one({"email": email}, {"_id": 0})
        if student:
            print(f"✅ Google Login (Student): {student['name']} ({email})")
            return LoginResponse(
                user_id=student["user_id"],
                user_type="student",
                name=student["name"],
                roll_number=student["roll_number"],
                email=student.get("email")
            )
        
        raise HTTPException(
            status_code=404, 
            detail="No account found for this Google email. Please sign up manually or use a registered faculty account."
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google token.")

# GOOGLE STUDENT SIGNUP (Complete Profile)
@api_router.post("/auth/google-signup-student", response_model=LoginResponse)
async def google_signup_student(request: GoogleStudentSignupRequest):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Server config error")
        
    try:
        idinfo = id_token.verify_oauth2_token(request.credential, google_requests.Request(), GOOGLE_CLIENT_ID)
        email = idinfo.get("email")
        name = idinfo.get("name")
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    # Check duplicates
    if await db.students.find_one({"email": email}):
        raise HTTPException(status_code=409, detail="Email already registered")
    if await db.students.find_one({"roll_number": request.roll_number}):
        raise HTTPException(status_code=409, detail="Roll number already registered")
    
    if await db.students.find_one({"phone": request.phone}):
        raise HTTPException(status_code=409, detail="Phone number already registered")

    student_data = {
        "user_id": str(uuid.uuid4()),
        "name": name,
        "email": email,
        "roll_number": request.roll_number,
        "phone": request.phone,
        "branch": request.branch,
        "year": request.year,
        "section": request.section,
        "user_type": "student",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.students.insert_one(student_data)
    print(f"✅ NEW Google Student: {name} ({request.roll_number})")
    
    return LoginResponse(
        user_id=student_data["user_id"],
        user_type="student",
        name=student_data["name"],
        roll_number=student_data["roll_number"],
        email=student_data["email"]
    )

# MENU
@api_router.get("/menu", response_model=List[MenuItem])
async def get_menu(category: Optional[str] = None):
    query = {}
    if category:
        query["category"] = category
    
    menu_items = await db.menu_items.find(query, {"_id": 0}).to_list(1000)
    return menu_items

# CART - ADD
@api_router.post("/cart/add")
async def add_to_cart(request: AddToCartRequest):
    cart = await db.carts.find_one({"user_id": request.user_id})
    
    if not cart:
        cart = {"user_id": request.user_id, "items": []}
    
    item_found = False
    for item in cart["items"]:
        if item["menu_item_id"] == request.menu_item_id:
            item["quantity"] += request.quantity
            item_found = True
            break
    
    if not item_found:
        cart["items"].append({
            "menu_item_id": request.menu_item_id,
            "quantity": request.quantity
        })
    
    await db.carts.update_one(
        {"user_id": request.user_id},
        {"$set": cart},
        upsert=True
    )
    
    return {"message": "Item added to cart"}

# CART - GET
@api_router.get("/cart/{user_id}", response_model=CartResponse)
async def get_cart(user_id: str):
    cart = await db.carts.find_one({"user_id": user_id}, {"_id": 0})
    
    if not cart:
        return CartResponse(user_id=user_id, items=[], total_amount=0.0)
    
    populated_items = []
    total_amount = 0.0
    
    for cart_item in cart["items"]:
        menu_item = await db.menu_items.find_one({"id": cart_item["menu_item_id"]}, {"_id": 0})
        if menu_item:
            item_total = menu_item["price"] * cart_item["quantity"]
            total_amount += item_total
            populated_items.append({
                "menu_item": menu_item,
                "quantity": cart_item["quantity"],
                "item_total": item_total
            })
    
    return CartResponse(user_id=user_id, items=populated_items, total_amount=total_amount)

# CART - UPDATE
@api_router.post("/cart/update")
async def update_cart_item(request: AddToCartRequest):
    cart = await db.carts.find_one({"user_id": request.user_id})
    
    if not cart:
        raise HTTPException(status_code=404, detail="Cart not found")
    
    if request.quantity == 0:
        cart["items"] = [item for item in cart["items"] if item["menu_item_id"] != request.menu_item_id]
    else:
        for item in cart["items"]:
            if item["menu_item_id"] == request.menu_item_id:
                item["quantity"] = request.quantity
                break
    
    await db.carts.update_one({"user_id": request.user_id}, {"$set": cart})
    return {"message": "Cart updated"}

# ORDER - PLACE
@api_router.post("/order/place", response_model=OrderResponse)
async def place_order(request: PlaceOrderRequest):
    cart = await db.carts.find_one({"user_id": request.user_id}, {"_id": 0})
    
    if not cart or not cart["items"]:
        raise HTTPException(status_code=400, detail="Cart is empty")
    
    order_items = []
    total_amount = 0.0
    
    for cart_item in cart["items"]:
        menu_item = await db.menu_items.find_one({"id": cart_item["menu_item_id"]}, {"_id": 0})
        if menu_item:
            item_total = menu_item["price"] * cart_item["quantity"]
            total_amount += item_total
            order_items.append({
                "menu_item": menu_item,
                "quantity": cart_item["quantity"],
                "item_total": item_total
            })
    
    # Add 5% Tax
    tax_amount = total_amount * 0.05
    total_amount += tax_amount
    
    order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
    order = {
        "order_id": order_id,
        "user_id": request.user_id,
        "items": order_items,
        "total_amount": total_amount,
        "status": "confirmed",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.orders.insert_one(order)
    await db.carts.delete_one({"user_id": request.user_id})
    
    return OrderResponse(**order)

# ADMIN - GET ALL ORDERS (Kitchen Dashboard)
@api_router.get("/admin/orders", response_model=List[OrderResponse])
async def get_all_orders():
    # Fetch all orders sorted by newest first
    orders = await db.orders.find({}, {"_id": 0}).sort("created_at", -1).to_list(100)
    
    enriched_orders = []
    for order in orders:
        # Fetch user info to display on dashboard
        user = await db.students.find_one({"user_id": order["user_id"]})
        if not user:
            user = await db.faculty.find_one({"user_id": order["user_id"]})
            
        order_data = order.copy()
        if user:
            order_data["user_name"] = user.get("name", "Unknown")
            # Use roll number for students, email for faculty
            if user.get("user_type") == "student":
                details = user.get("roll_number", "N/A")
                if user.get("branch"):
                    details += f" | {user.get('branch')}"
                if user.get("year"):
                    details += f" - {user.get('year')} Yr"
                order_data["user_details"] = details
            else:
                order_data["user_details"] = user.get("email")
            
            order_data["user_phone"] = user.get("phone")
        else:
            order_data["user_name"] = "Unknown User"
            order_data["user_details"] = "N/A"
            order_data["user_phone"] = None
            
        enriched_orders.append(order_data)
        
    return enriched_orders

# ADMIN - UPDATE ORDER STATUS
@api_router.post("/admin/order/update-status")
async def update_order_status(request: UpdateOrderStatusRequest):
    result = await db.orders.update_one(
        {"order_id": request.order_id},
        {"$set": {"status": request.status}}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Order not found")
    return {"message": f"Order marked as {request.status}"}

# ORDER - GET
@api_router.get("/order/{order_id}", response_model=OrderResponse)
async def get_order(order_id: str):
    order = await db.orders.find_one({"order_id": order_id}, {"_id": 0})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return OrderResponse(**order)

# ORDERS - GET USER HISTORY
@api_router.get("/orders/user/{user_id}", response_model=List[OrderResponse])
async def get_user_orders(user_id: str):
    orders = await db.orders.find({"user_id": user_id}, {"_id": 0}).sort("created_at", -1).to_list(100)
    return orders

# AI CHAT
@api_router.post("/chat")
async def chat_with_ai(request: ChatRequest):
    if not genai:
        return {"response": "AI features are currently unavailable (Server missing google-generativeai library)."}
        
    if not GEMINI_API_KEY:
        return {"response": "AI is currently sleeping (API Key missing). Please contact admin."}

    try:
        # 1. Fetch menu context for the AI
        menu_items = await db.menu_items.find({}, {"_id": 0, "name": 1, "price": 1, "category": 1, "available": 1}).to_list(1000)
        
        # 2. Format menu for the prompt
        menu_text = "\n".join([f"- {m['name']} ({m['category']}): ₹{m['price']}" for m in menu_items if m.get('available')])
        
        # Add weather context
        weather_instruction = ""
        if request.weather:
            weather_instruction = f"\nCurrent local weather: {request.weather}. Please suggest food/drinks appropriate for this weather (e.g., hot spicy food for rain, cool drinks for summer)."

        # 3. Construct System Prompt
        system_instruction = f"""You are 'FoodieBot', a helpful AI assistant for the Smart Canteen college app.
        
        Here is the current menu:
        {menu_text}
        {weather_instruction}
        
        Rules:
        1. Answer questions about menu items, prices, and give recommendations based on the menu above.
        2. Keep answers short, friendly, and appetizing.
        3. If asked about things unrelated to food or the canteen, politely refuse.
        """
        
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = await model.generate_content_async(f"{system_instruction}\n\nUser: {request.message}\nAI:")
        return {"response": response.text}
    except Exception as e:
        print(f"Gemini Error: {e}")
        return {"response": f"I'm having trouble connecting. Error: {str(e)}"}

# Include router
app.include_router(api_router)

# ✅ FIXED CORS - Works with ALL ports
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173", 
        "http://127.0.0.1:5173",
        "*"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown():
    client.close()

# Startup - Indexes + Seed
@app.on_event("startup")
async def startup():
    # Indexes
    await db.students.create_index("roll_number", unique=True)
    await db.faculty.create_index("email", unique=True)
    await db.carts.create_index("user_id")
    
    # Check if we need to update the menu (if "Veg Rice" is missing, it's likely the old menu)
    if await db.menu_items.count_documents({}) > 0:
        if not await db.menu_items.find_one({"name": "Veg Rice"}):
            print("♻️ Detected outdated menu data. Clearing to update with full menu...")
            await db.menu_items.drop()

    # Seed menu (if empty)
    if await db.menu_items.count_documents({}) == 0:
        # Raw data from frontend menuData.js
        raw_items = [
            # Rice
            {"name": "Veg Rice", "price": 70, "type": "veg", "image": "https://github.com/contactvihar-cpu/connect/blob/main/veg%20rice-70.jpg?raw=true"},
            {"name": "Gobi Rice", "price": 80, "type": "veg", "image": "https://github.com/contactvihar-cpu/connect/blob/main/gobi%20rice-80.jpg?raw=true"},
            {"name": "Sweet Corn Rice", "price": 80, "type": "veg", "image": "https://github.com/contactvihar-cpu/connect/blob/main/sweet-corn-rice-80.webp?raw=true"},
            {"name": "Jeera Rice", "price": 80, "type": "veg", "image": "https://github.com/contactvihar-cpu/connect/blob/main/Jeera-Rice-80.png?raw=true"},
            {"name": "Baby Corn Rice", "price": 90, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/Baby-Corn-Fried-Rice-90.jpg?raw=true"},
            {"name": "Mushroom Rice", "price": 100, "type": "veg", "image": " https://github.com/contactvihar-cpu/connect/blob/main/mushroom-rice-100.jpg?raw=true "},
            {"name": "Ghee Rice", "price": 100, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/Ghee_Rice_100.webp?raw=true"},
            {"name": "Cashew Rice", "price": 100, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/cashew-rice-100.webp?raw=true"},
            {"name": "Paneer Rice", "price": 100, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/Paneer-Fried-Rice-100.webp?raw=true"},
            {"name": "Egg Rice", "price": 70, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/egg%20rice-70.jpg?raw=true"},
            {"name": "Chicken Rice", "price": 110, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/chicken%20rice-110.jpg?raw=true"},
            # Noodles
            {"name": "Veg Noodles", "price": 70, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/veg%20noodles-70.jpg?raw=true"},
            {"name": "Gobi Noodles", "price": 80, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/gobi%20noodles-80.jpg?raw=true"},
            {"name": "Sweet Corn Noodles", "price": 80, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/sweet%20corn%20noodles-80.png?raw=true"},
            {"name": "Baby Corn Noodles", "price": 90, "type": "veg", "image": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcQ5ItqDyFuqUcrzyeUuBpXjxax1wJHQQJLMeA&s"},
            {"name": "Mushroom Noodles", "price": 100, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/mushroom%20noodles-100.webp?raw=true"},
            {"name": "Paneer Noodles", "price": 100, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/paneer%20noodles-100.jpg?raw=true"},
            {"name": "Egg Noodles", "price": 70, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/egg%20noodles-70.jpg?raw=true"},
            {"name": "Chicken Noodles", "price": 110, "type": "nonveg", "image": "https://static.toiimg.com/thumb/54458787.cms?imgsize=153197&width=800&height=800"},
            # Biryani
            {"name": "Chicken Biryani", "price": 150, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/chicken%20biriyani-150.jpg?raw=true"},
            {"name": "Lollipop Biryani (3 pcs)", "price": 160, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/chicken%20lollipop%20biriyani(pieces-3)-160.jpg?raw=true"},
            {"name": "Leg Piece Biryani (2 pcs)", "price": 190, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/leg%20piece%20biriyani(2-piece)-190.jpg?raw=true"},
            {"name": "Boneless Biryani", "price": 190, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/Boneless-Chicken-Biryani-190.jpg?raw=true"},
            {"name": "Mini Biryani", "price": 110, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/mini%20biriyani-110.jpg?raw=true"},
            {"name": "Egg Biryani", "price": 120, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/-Egg-Biryani-120.jpg?raw=true"},
            {"name": "Dry Lollipops (3 pcs)", "price": 100, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/dry%20lollipop%203-pieces-100.jpg?raw=true"},
            {"name": "Veg Biryani", "price": 120, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/veg%20biriyani-120.jpg?raw=true"},
            # Special Rice
            {"name": "Special Gobi Rice", "price": 120, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/special-gobi-rice-120.webp?raw=true"},
            {"name": "Special Mushroom Rice", "price": 130, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/special%20mushroom-fried-rice-130.webp?raw=true"},
            {"name": "Special Paneer Rice", "price": 130, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/special%20paneer-fried-rice-130.jpg?raw=true"},
            {"name": "Special Egg Rice", "price": 120, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/special%20egg-fried-rice-120.jpg?raw=true"},
            {"name": "Special Chicken Rice", "price": 180, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/special%20chicken%20rice-180.jpg?raw=true"},
            {"name": "Triple Fried Rice Veg", "price": 150, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/triple%20fried%20rice%20veg-150.jpg?raw=true"},
            {"name": "Triple Fried Chicken Rice", "price": 190, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/triple%20fried%20chicken%20rice-190.jpg?raw=true"},
            {"name": "Schezwan Gobi Rice", "price": 100, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/schezwaan%20Gobi-Fried-Rice-100.jpg?raw=true"},
            {"name": "Schezwan Egg Rice", "price": 90, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/schezwaan%20egg%20rice.webp?raw=true"},
            {"name": "Schezwan Chicken Rice", "price": 130, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/schezwaan%20chicken%20rice-130.jpg?raw=true"},
            {"name": "Schezwan Chicken Noodles", "price": 130, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/schezwaan%20chicken%20noodles-130.jpg?raw=true"},
            # Tiffins
            {"name": "Dosa", "price": 20, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/dosa-20.jpg?raw=true"},
            {"name": "Uthappam", "price": 30, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/utham-30.jpg?raw=true"},
            {"name": "Karam Dosa", "price": 30, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/karam-dosa-30.jpg?raw=true"},
            {"name": "Masala Dosa", "price": 40, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/masala%20dosa-40.jpg?raw=true"},
            {"name": "Onion Dosa", "price": 40, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/onion%20dosa-40.jpg?raw=true"},
            {"name": "Ghee Dosa", "price": 50, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/ghee%20dosa-50.jpg?raw=true"},
            {"name": "Egg Dosa", "price": 50, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/egg%20dosa-50.webp?raw=true"},
            {"name": "Poori", "price": 30, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/poori-30.webp?raw=true"},
            # Curries
            {"name": "Veg Curry", "price": 100, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/veg%20curry-100.jpg?raw=true"},
            {"name": "Egg Curry", "price": 60, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/Egg-Curry-60.jpg?raw=true"},
            {"name": "Mushroom Curry", "price": 130, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/mushroom%20curry-130.JPG?raw=true"},
            {"name": "Paneer Curry", "price": 140, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/Paneer-Curry-140.jpg?raw=true"},
            {"name": "Chicken Curry (Full)", "price": 150, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/chicken%20curry%20full-150.jpg?raw=true"},
            {"name": "Chicken Curry (Half)", "price": 100, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/Chicken-Curry-half-100.webp?raw=true"},
            {"name": "Dal Rice", "price": 70, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/dal%20rice-70.jpg?raw=true"},
            {"name": "Tomato Rice", "price": 70, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/Tomato-Rice-70.webp?raw=true"},
            {"name": "Lemon Rice", "price": 60, "type": "veg", "image": "https://www.indianveggiedelight.com/wp-content/uploads/2023/03/lemon-rice-stovetop-featured.jpg"},
            {"name": "Curd Rice", "price": 60, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/curd%20rice-60.jpg?raw=true"},
            # Starters
            {"name": "Gobi Manchurian", "price": 110, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/gobi%20manjuria-110.webp?raw=true"},
            {"name": "Chilli Gobi", "price": 120, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/chilli%20gobi-120.jpg?raw=true"},
            {"name": "Gobi 65", "price": 120, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/gobi-65-120.webp?raw=true"},
            {"name": "Aloo Gobi", "price": 110, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/aloo%20gobi-110.jpg?raw=true"},
            {"name": "Mushroom Manchurian", "price": 150, "type": "veg", "image": "https://images.archanaskitchen.com/images/recipes/world-recipes/indian-chinese-recipes/Mushroom_Manchurian_Recipe_Dry_Indo_Chinese_Indian_Chinese_10_a0e1d8f782.jpg"},
            {"name": "Paneer Manchurian", "price": 150, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/paneer-manchurian-150.jpg?raw=true"},
            {"name": "Paneer Chilli", "price": 160, "type": "veg", "image": "https://signatureconcoctions.com/wp-content/uploads/2023/11/pasted-image-0-7.png"},
            {"name": "Egg Chilli", "price": 140, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/egg%20chilli-140.jpg?raw=true"},
            {"name": "Chicken Manchurian", "price": 180, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/chicken%20manchurian-180.jpg?raw=true"},
            {"name": "Chicken 65", "price": 190, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/chicken-65-190.jpg?raw=true"},
            {"name": "Chilli Chicken", "price": 190, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/CHILLICHICKEN_190.webp?raw=true"},
            # Snacks
            {"name": "Samosa", "price": 20, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/samosa-20.jpg?raw=true"},
            {"name": "Bread Patties", "price": 25, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/bread%20patties-25.jpg?raw=true"},
            {"name": "Bread Omelette", "price": 50, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/bread-omelette-50.webp?raw=true"},
            {"name": "Pani Puri", "price": 30, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/pani%20puri-30.jpg?raw=true"},
            {"name": "Samosa Chat", "price": 50, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/samosa%20chat-50.jpg?raw=true"},
            {"name": "Gobi Chat", "price": 50, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/gobi%20chat-50.jpg?raw=true"},
            {"name": "Gulab Jamun", "price": 15, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/gulab%20jamun-15.jpg?raw=true"},
            {"name": "Parota", "price": 50, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/parotta-50.webp?raw=true"},
            {"name": "Egg Parota", "price": 50, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/egg%20parota-50.jpg?raw=true"},
            {"name": "Chapati", "price": 50, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/chapathi-50.jpg?raw=true"},
            # Rolls
            {"name": "Veg Roll", "price": 60, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/veg%20roll-60.jpg?raw=true"},
            {"name": "Gobi Roll", "price": 70, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/gobi%20roll-70.jpg?raw=true"},
            {"name": "Paneer Roll", "price": 80, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/paneer%20roll-80.jpg?raw=true"},
            {"name": "Egg Roll", "price": 60, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/egg%20roll-60.webp?raw=true"},
            {"name": "Chicken Roll", "price": 80, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/chicken%20roll-80.jpg?raw=true"},
            {"name": "Special Gobi Roll", "price": 90, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/special%20gobi%20roll-90.webp?raw=true"},
            {"name": "Special Egg Roll", "price": 80, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/special%20egg%20roll-90.webp?raw=true"},
            {"name": "Special Chicken Roll", "price": 100, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/special%20chicken%20roll-100.jpg?raw=true"},
            {"name": "Omelette", "price": 40, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/omlette-40.jpg?raw=true"},
            # Sandwiches
            {"name": "Veg Sandwich", "price": 50, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/veg%20sandwich-50.jpg?raw=true"},
            {"name": "Egg Sandwich", "price": 60, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/egg%20sandwich-60.jpg?raw=true"},
            {"name": "Chicken Sandwich", "price": 80, "type": "nonveg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/chicken%20sandwich-80.jpg?raw=true"},
            {"name": "Garlic Sandwich", "price": 60, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/garlic%20sandwich-60.jpg?raw=true"},
            # Meals
            {"name": "Mini Meals", "price": 60, "type": "veg", "image": "https://thehomecookings.com/wp-content/uploads/2025/03/veg-mini.jpeg"},
            {"name": "Meals", "price": 90, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/meals-90.jpg?raw=true"},
            {"name": "Executive Meals", "price": 120, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/executive%20meals-120.webp?raw=true"},
            {"name": "Meals Parcel", "price": 150, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/meals%20parsel-150.jpg?raw=true"},
            # Beverages
            {"name": "Tea", "price": 13, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/tea-13.webp?raw=true"},
            {"name": "Coffee", "price": 15, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/coffee-15.jpg?raw=true"},
            {"name": "Green Tea", "price": 20, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/green%20tea-20.jpg?raw=true"},
            {"name": "Lemon Tea", "price": 20, "type": "veg", "image": "https://images.ctfassets.net/v601h1fyjgba/vLTpiu7GXnZotw9x7azqb/4c638af4f2df11f572d92bfe2c46e2cc/LS_IMG_IST_Chamomile_Tea_Lemon_Hi.jpg"},
            {"name": "Milk", "price": 15, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/milk-15.jpg?raw=true"},
            # Fruit Juices
            {"name": "Watermelon", "price": 30, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/watermelon%20fruit%20juice-30.webp?raw=true"},
            {"name": "Mosambi", "price": 30, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/mosambi%20fruit%20juice-30.jpg?raw=true"},
            {"name": "Orange", "price": 40, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/orange%20fruit%20juice-40.jpeg?raw=true"},
            # Shakes
            {"name": "Banana", "price": 40, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/banana%20shake-40.png?raw=true"},
            {"name": "Mango", "price": 40, "type": "veg", "image": "https://github.com/contactvihar-cpu/canteen/blob/main/mango%20shake-40.jpg?raw=true"},
        ]
        
        menu_items = []
        for item in raw_items:
            menu_items.append({
                "id": str(uuid.uuid4()),
                "name": item["name"],
                "description": f"Delicious {item['name']} made with fresh ingredients",
                "price": item["price"],
                "category": "non-veg" if item["type"] == "nonveg" else "veg",
                "image_url": item["image"],
                "available": True
            })
            
        await db.menu_items.insert_many(menu_items)
        print(f"✅ Seeded {len(menu_items)} menu items")
    
    # Seed demo faculty (if empty)
    if await db.faculty.count_documents({}) == 0:
        hashed = bcrypt.hashpw("faculty123".encode(), bcrypt.gensalt()).decode()
        await db.faculty.insert_one({
            "user_id": str(uuid.uuid4()),
            "name": "Dr. Smith",
            "email": "faculty@college.edu",
            "password": hashed,
            "user_type": "faculty"
        })
        print("✅ Seeded demo faculty: faculty@college.edu / faculty123")
    
    print("🚀 Smart Canteen API Ready! http://localhost:8000/docs")
