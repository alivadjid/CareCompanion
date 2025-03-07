import hashlib
from typing import Optional
from fastapi import FastAPI
from fastapi.exceptions import WebSocketException
from fastapi import WebSocket, WebSocketDisconnect
from networking import ClientConnectionManager
import fastapi as _fastapi
import fastapi.security as _security
import services as _services
import schemas as _schemas
import sqlalchemy.orm as _orm
import models
import websockets
import json
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from gemini_handler import GeminiClient
import os
from dotenv import load_dotenv
from fastapi.security import APIKeyHeader



api_key_header = APIKeyHeader(name="API_KEY")
app = FastAPI()
# Define the allowed origins
origins = [
    # "https://carecompanion-production.up.railway.app"
    "http://localhost:3005"
]

# Configure CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
websocket_manager = ClientConnectionManager()

# Load the API key from an environment variable
load_dotenv()

# Create a Gemini client instance
gemini_api_key = os.getenv('GEMINI_API_KEY', 'default_key_if_not_set')
gemini_client = GeminiClient(gemini_api_key)
gemini_client.set_instructions()




@app.get('/')
async def root():
    return {"Message":"CareCompanion API "}
# User authentication endpoints


@app.post("/api/users")
async def create_user(
    user: _schemas.UserCreate, 
    db: _orm.Session = _fastapi.Depends(_services.get_db),
    register_api_key: Optional[str] = _fastapi.Depends(api_key_header)
):
    """ Endpoint responsible for creating users"""
    # Replace "thetechnicalhackers321" with the hashed key
    hashed_key = hashlib.sha256("thetechnicalhackers321".encode()).hexdigest()
    if register_api_key != hashed_key:
        raise _fastapi.HTTPException(status_code=401, detail="Invalid API key")

    db_user = await _services.get_user_by_email(db, user.email)
    if db_user:
        raise _fastapi.HTTPException(status_code=400, detail="Email already in use")
    
    return {await _services.create_user(db, user)
}

@app.post("/api/token")
async def generate_token(
    form_data: _security.OAuth2PasswordRequestForm = _fastapi.Depends(),
    db: _orm.Session = _fastapi.Depends(_services.get_db),
):
    """ Endpoint responsible for generating token """
    user = await _services.authenticate_user(form_data.username, form_data.password, db)

    if not user:
        raise _fastapi.HTTPException(status_code=401, detail="Invalid Credentials")

    return await _services.create_token(user)


@app.get("/api/users/me", response_model=_schemas.User)
async def get_user(
    user:_schemas.User = _fastapi.Depends(_services.get_current_user),
    ):
    """ Endpoint responsible to get the user"""
    return user


# Endpoint to create a conversation
@app.post("/api/create_convo/", response_model=dict)
async def create_conversation(
    conversation: _schemas.ConversationCreate,
    db: _orm.Session = _fastapi.Depends(_services.get_db),
    token: str = _fastapi.Depends(_services.authenticate_token),
    ):
    """Endpoint used to create a conversation """
    user = await _services.get_current_user(db, token)
    convo = await _services.create_conversation_service(user, conversation, db)
    return {
        "conversation_id":convo.id, 
        'conversation_date_created':convo.date_created ,
        "message": "Conversation created successfully"
        }


# Endpoint to get all conversation user has
@app.get('/api/convos/{user_id}')
async def get_user_conversation(
    user_id:int,
    db: _orm.Session = _fastapi.Depends(_services.get_db),
    token: str = _fastapi.Depends(_services.authenticate_token),
    ):
    """ Get al the user conversations"""
    user = await _services.get_current_user(db, token)
    user_conversations = await _services.get_user_conversations(db, user.id)
    if not user:
        raise  _fastapi.HTTPException(status_code=404, detail="Conversation not found")
    return user_conversations


@app.get('/api/chat_history/{room_id}')
async def get_message_from_conversation(
    room_id:str,
    db: _orm.Session = _fastapi.Depends(_services.get_db),
    token: str = _fastapi.Depends(_services.authenticate_token),
    ):

    """ Get all the messages from  conversations"""
    conversation = await _services.get_conversation_by_id(db, room_id)

    if not conversation:
        _fastapi.HTTPException(status_code=404, detail="Conversation not found")
    messages = await _services.get_all_messages_from_conversation(db, conversation.id)
    message_payload_schema = [_schemas.MessageSchema(**message.__dict__) for message in messages]

    gemini_client.set_chat_history(message_payload_schema)
    
    return message_payload_schema


@app.websocket("/api/chat/{room_id}/{token}")
async def chat_endpoint(
    room_id:str,
    token:str,
    websocket:WebSocket,
    db:_orm.Session = _fastapi.Depends(_services.get_db)
    ):
    await websocket.accept()
    user = await _services.verify_socket_connection(token, db=db)
    conversation = await _services.check_conversation_exists(db, room_id)
    if not conversation:
        await websocket.send_json({"message":'conversation does not exist'})
    await websocket_manager.connect(room_id, websocket)
    while True:
        try:
            # Receive JSON data containing the message payload
            user_input = await websocket.receive_text()
            websocket_conn = websocket_manager.active_connections[room_id]

            await websocket_conn.send_json({
                "text_content": user_input,
                "is_bot_message": False,
            })

            # Get the response from the Gemini API
            response = gemini_client.get_response(user_input)
            
            # Send the AI's response back to the client via WebSocket
            bot_response = {
                    "text_content": response,
                    "is_bot_message": True,
                }
            await websocket_conn.send_json(bot_response)

            new_message = models.Message(
                text_content=user_input,
                author_id=user.id,
                conversation_id=room_id
            )
            db.add(new_message)
            db.commit()

            # add the message record in db
            bot_message = models.Message(
                text_content=bot_response["text_content"],
                conversation_id=room_id,
                is_bot_message=True,
                
                )
            db.add(bot_message)
            db.commit()

        except websockets.exceptions.ConnectionClosedOK as e:
            websocket_manager.disconnect(room_id)
         
        except websockets.exceptions.ConnectionClosedError as error:
            websocket_manager.disconnect(room_id)
        except json.decoder.JSONDecodeError:
            # if user does not put in the format of json
            websocket_manager.disconnect(room_id)
            raise WebSocketException(code=_fastapi.status.WS_1008_POLICY_VIOLATION, reason="Unable to parse JSON")
        except WebSocketDisconnect:
            websocket_manager.disconnect(room_id)
        
        except Exception as e:
            websocket_manager.disconnect(room_id)
            break


if __name__ == "__main__":
    import uvicorn

    # Run the server using uvicorn when this script is executed directly
    uvicorn.run(app, host="0.0.0.0", port=8000)