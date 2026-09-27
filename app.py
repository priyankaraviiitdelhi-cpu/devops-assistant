"""
app.py - the main web page (built with Streamlit).
Run it with:  streamlit run app.py
"""
import streamlit as st

import infra_agent
import tools_agent
import ui_cost
import ui_provision
from llm import MODEL, PROVIDER, chat
from rag import search

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
use_tools = st.sidebar.toggle("Use live AWS tools + architect", value=True,
                              help="Reads your AWS account, suggests architectures with cost estimates. "
                                   "Anything that changes AWS needs your confirmation.")
if st.sidebar.button("Clear chat"):
    st.session_state.messages = []
    st.session_state.pending_actions = []
    st.session_state.pop("infra", None)

if not st.session_state.messages:
    st.info("Try: *I'm building a registration website for a college hackathon. About 300 students "
            "for 2 weeks, they upload a profile photo. My budget is $10.*")


def show_sources(sources):
    """Show which knowledge base chunks were used for an answer."""
    if sources:
        with st.expander(f"Sources ({len(sources)})"):
            for s in sources:
                st.markdown(f"**{s['source']}**")
                st.caption(s["text"][:300] + " ...")


def show_tools(tools):
    """Show which tools the AI called."""
    if tools:
        st.caption("Tools used: " + ", ".join(f"`{t}`" for t in tools))


def finish_action(action, message):
    st.session_state.messages.append({"role": "assistant", "content": message})
    st.session_state.pending_actions.remove(action)
    st.rerun()


# Show the previous messages
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        show_tools(m.get("tools"))
        show_sources(m.get("sources"))

# Actions waiting for the user's confirmation
for i, action in enumerate(list(st.session_state.pending_actions)):
    with st.container(border=True):
        if action.get("kind") == "provision":
            st.info(f"**Deployment request:** {action['summary']}\n\n"
                    f"Estimated cost: **${action['estimated_cost_usd']:.2f}** for {action['duration_days']:g} days")
            c1, c2 = st.columns(2)
            if c1.button("Confirm - generate Terraform plan", key=f"confirm-{i}", type="primary"):
                with st.spinner("Writing Terraform and running plan (about 1-2 minutes)..."):
                    try:
                        st.session_state.infra = infra_agent.prepare(
                            action["request"], duration_days=action["duration_days"])
                        msg = "The Terraform plan is ready below. Review it, then click **Approve and apply**."
                    except Exception as e:
                        msg = f"Failed to prepare the plan: {e}"
                finish_action(action, msg)
            if c2.button("Cancel", key=f"cancel-{i}"):
                finish_action(action, "Cancelled the deployment request.")
        else:
            label = f"{action['action'].upper()} instance {action['instance_id']}"
            if action["name"]:
                label += f" ({action['name']})"
            st.warning(f"Confirm: {label}? It is currently **{action['current_state']}**.")
            c1, c2 = st.columns(2)
            if c1.button("Confirm", key=f"confirm-{i}", type="primary"):
                try:
                    result = tools_agent.execute_action(action)
                except Exception as e:
                    result = f"Failed: {e}"
                finish_action(action, result)
            if c2.button("Cancel", key=f"cancel-{i}"):
                finish_action(action, f"Cancelled: {label}.")

# Terraform plan created from the chat
if "infra" in st.session_state:
    with st.container(border=True):
        st.markdown("### Deployment plan")
        ui_provision._show_proposal(st.session_state.infra)
        st.caption("After applying, manage or destroy it on the **Provision infrastructure** page.")

# Chat input box at the bottom
if prompt := st.chat_input("Describe your project, ask a DevOps question, or ask about your AWS account..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"), st.spinner("Thinking..."):
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
                {"role": "assistant", "content": reply, "sources": sources, "tools": tools})
            if pending:
                st.session_state.pending_actions = pending
                st.rerun()
        except Exception as e:
            st.session_state.messages.pop()
            st.error(f"Something went wrong: {e}")