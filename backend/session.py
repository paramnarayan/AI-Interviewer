class InterviewSession:
    def __init__(self, session_id:str ="default"): 
        self.audio_buffer= bytearray()
        self.history:list[dict] = []
        self.session_id = session_id

    def add_user_turn( self,text:str):
        self.history.append({"role": "user", "content": text})

    def add_assistant_turn(self, text: str):
        self.history.append({"role": "assistant", "content": text})
