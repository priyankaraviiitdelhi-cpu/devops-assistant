"""
app.py - the main web page (built with Streamlit).
Run it with:  streamlit run app.py
"""
import streamlit as st
from llm import chat, PROVIDER, MODEL
from rag import search
import tools_agent
import ui_provision
import ui_cost

st.set_page_config(page_title="AI DevOps Assistant")
st.title("AI DevOps Assistant")
st.caption(f"Powered by {PROVIDER} / {MODEL}")

# ---- Sidebar: choose a mode ----
st.sidebar.header("Mode")
mode = st.sidebar.radio("What do you want to do?", ["Chat", "Provision infrastructure", "Cost optimization"])

if mode == "Provision infrastructure":
    ui_provision.render()
    st.stop()

if mode == "Cost optimization":
    ui_cost.render()
    st.stop()

# ---- Chat mode ----
if "messages" not in st.session_state:
    st.session_state.messages = []
if "pending_actions" not in st.session_state:
    st.session_state.pending_actions = []

st.sidebar.header("Chat settings")
use_kb = st.sidebar.toggle("Use knowledge base (RAG)", value=True)
use_tools = st.sidebar.toggle("Use live AWS tools", value=True,
                              help="Lets the assistant read your real AWS account. Start/stop needs your confirmation.")
if st.sidebar.button("Clear chat"):
    st.session_state.messages = []
    st.session_state.pending_actions = []


def show_sources(sources):
    """Show which knowledge base chunks were used for an answer."""
    if sources:
        with st.expander(f"Sources ({len(sources)})"):
            for s in sources:
                st.markdown(f"**{s['source']}**")
                st.caption(s["text"][:300] + " ...")


def show_tools(tools):
    """Show which AWS tools the AI called."""
    if tools:
        st.caption("Tools used: " + ", ".join(f"`{t}`" for t in tools))


# Show the previous messages
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        show_tools(m.get("tools"))
        show_sources(m.get("sources"))

# Actions waiting for the user's confirmation
for i, action in enumerate(list(st.session_state.pending_actions)):
    label = f"{action['action'].upper()} instance {action['instance_id']}"
    if action["name"]:
        label += f" ({action['name']})"
    with st.container(border=True):
        st.warning(f"Confirm: {label}? It is currently **{action['current_state']}**.")
        c1, c2 = st.columns(2)
        if c1.button("Confirm", key=f"confirm-{i}", type="primary"):
            try:
                result = tools_agent.execute_action(action)
            except Exception as e:
                result = f"Failed: {e}"
            st.session_state.messages.append({"role": "assistant", "content": result})
            st.session_state.pending_actions.remove(action)
            st.rerun()
        if c2.button("Cancel", key=f"cancel-{i}"):
            st.session_state.messages.append({"role": "assistant", "content": f"Cancelled: {label}."})
            st.session_state.pending_actions.remove(action)
            st.rerun()

# Chat input box at the bottom
if prompt := st.chat_input("Ask a DevOps question or about your AWS account..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                sources = search(prompt) if use_kb else []
                context = "\n\n---\n\n".join(f"[{s['source']}]\n{s['text']}" for s in sources)
                tools, pending = [], []
                if use_tools:
                    reply, tools, pending = tools_agent.run(st.session_state.messages, context or None)
                else:
                    reply = chat(st.session_state.messages, context=context or None)
                st.markdown(reply)
                show_tools(tools)
                show_sources(sources)
                st.session_state.messages.append(
                    {"role": "assistant", "content": reply, "sources": sources, "tools": tools}
                )
                if pending:
                    st.session_state.pending_actions = pending
                    st.rerun()
            except Exception as e:
                st.session_state.messages.pop()
                st.error(f"Something went wrong: {e}")