"""
app.py - the chat web page (built with Streamlit).
Run it with:  streamlit run app.py
"""
import streamlit as st
from llm import chat, PROVIDER, MODEL

st.set_page_config(page_title="AI DevOps Assistant")
st.title("AI DevOps Assistant")
st.caption(f"Powered by {PROVIDER} / {MODEL}")

# Remember the conversation while the page is open
if "messages" not in st.session_state:
    st.session_state.messages = []

# Show the previous messages
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

# Chat input box at the bottom
if prompt := st.chat_input("Ask a DevOps or AWS question..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                reply = chat(st.session_state.messages)
                st.markdown(reply)
                st.session_state.messages.append({"role": "assistant", "content": reply})
            except Exception as e:
                st.session_state.messages.pop()  # remove the failed question
                st.error(f"Something went wrong: {e}")