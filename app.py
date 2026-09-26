"""
app.py - the chat web page (built with Streamlit).
Run it with:  streamlit run app.py
"""
import streamlit as st
from llm import chat, PROVIDER, MODEL
from rag import search

st.set_page_config(page_title="AI DevOps Assistant")
st.title("AI DevOps Assistant")
st.caption(f"Powered by {PROVIDER} / {MODEL}")

# Remember the conversation while the page is open
if "messages" not in st.session_state:
    st.session_state.messages = []

# Sidebar controls
st.sidebar.header("Settings")
use_kb = st.sidebar.toggle("Use knowledge base (RAG)", value=True)
if st.sidebar.button("Clear chat"):
    st.session_state.messages = []


def show_sources(sources):
    """Show which knowledge base chunks were used for an answer."""
    if sources:
        with st.expander(f"Sources ({len(sources)})"):
            for s in sources:
                st.markdown(f"**{s['source']}**")
                st.caption(s["text"][:300] + " ...")


# Show the previous messages
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        show_sources(m.get("sources"))

# Chat input box at the bottom
if prompt := st.chat_input("Ask a DevOps or AWS question..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Searching knowledge base and thinking..."):
            try:
                sources = search(prompt) if use_kb else []
                context = "\n\n---\n\n".join(f"[{s['source']}]\n{s['text']}" for s in sources)
                reply = chat(st.session_state.messages, context=context or None)
                st.markdown(reply)
                show_sources(sources)
                st.session_state.messages.append(
                    {"role": "assistant", "content": reply, "sources": sources}
                )
            except Exception as e:
                st.session_state.messages.pop()  # remove the failed question
                st.error(f"Something went wrong: {e}")