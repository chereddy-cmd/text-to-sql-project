import streamlit as st
import pandas as pd
import json
from google import genai
from google.genai import types
from sqlalchemy import create_engine, inspect
import requests
import uuid

# 1. Setup the Gemini Client
@st.cache_resource
def get_genai_client():
    return genai.Client(
        vertexai=True,
        project='gd-gcp-gridu-genai',
        location='us-central1'
    )
client = get_genai_client()

# 2. Setup the Database Connection
DB_URL = "postgresql://postgres:mysecretpassword@localhost:5432/postgres"
engine = create_engine(DB_URL)

# 3. Direct Langfuse API Tracker (Simple REST API)
def send_to_langfuse(user_question, prompt, ai_response):
    trace_id = str(uuid.uuid4())
    
    # These are your EXACT keys from the 'pe' project
    public_key = "use your public key"
    secret_key = "use your secret key"
    
    try:
        # 1. Send the Trace
        trace_url = "https://us.cloud.langfuse.com/api/public/traces"
        trace_payload = {
            "id": trace_id,
            "name": "Text-to-SQL Query",
            "input": user_question,
            "output": ai_response
        }
        res1 = requests.post(trace_url, json=trace_payload, auth=(public_key, secret_key))
        
        # 2. Send the AI Generation Step
        gen_url = "https://us.cloud.langfuse.com/api/public/generations"
        gen_payload = {
            "traceId": trace_id,
            "name": "gemini-2.5-flash-sql",
            "model": "gemini-2.5-flash",
            "input": prompt,
            "output": ai_response
        }
        res2 = requests.post(gen_url, json=gen_payload, auth=(public_key, secret_key))
        
        # Display the result on the Streamlit screen
        if res1.status_code == 200 and res2.status_code == 200:
            st.success("✅ Langfuse Trace Successfully Uploaded!")
        else:
            st.error(f"⚠️ Langfuse API Error - Trace: {res1.text} | Gen: {res2.text}")
            
    except Exception as e:
        st.error(f"⚠️ Langfuse Connection Error: {e}")

# 4. Build the UI Sidebar
st.sidebar.title("App Navigation")
page = st.sidebar.radio("Go to", ["Data Generation", "Talk to your data"])

# ==========================================
# PHASE 1: DATA GENERATION TAB
# ==========================================
if page == "Data Generation":
    st.title("Synthetic Data Generator")
    st.write("Upload a DDL schema and generate realistic synthetic data.")

    uploaded_file = st.file_uploader("Upload DDL Schema (.sql, .txt, .ddl)", type=["sql", "txt", "ddl"])
    user_instructions = st.text_area("Additional Instructions (e.g., 'Make all users from New York')")
    temperature = st.slider("Creativity (Temperature)", min_value=0.0, max_value=2.0, value=0.7, step=0.1)

    if st.button("Generate Data", key="generate_btn"):
        if uploaded_file is not None:
            ddl_content = uploaded_file.getvalue().decode("utf-8")
            
            with st.spinner("Gemini is generating synthetic data..."):
                try:
                    prompt = f"""
                    You are a synthetic data generator. I will provide a SQL DDL schema and instructions.
                    Your job is to generate 5 rows of highly realistic synthetic data for each table.
                    Ensure foreign keys match correctly across tables.
                    
                    DDL Schema:
                    {ddl_content}
                    
                    Special Instructions:
                    {user_instructions}
                    
                    Return the result as a valid JSON object where the keys are the table names, 
                    and the values are lists of dictionaries representing the rows.
                    """
                    
                    response = client.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            temperature=temperature,
                            response_mime_type="application/json",
                        )
                    )
                    
                    generated_data = json.loads(response.text)
                    st.success("Data generated successfully!")
                    
                    for table_name, rows in generated_data.items():
                        st.subheader(f"Table: {table_name}")
                        df = pd.DataFrame(rows)
                        st.dataframe(df)
                        
                        df.to_sql(table_name, engine, if_exists='replace', index=False)
                        st.success(f"✅ Saved '{table_name}' directly to PostgreSQL database!")
                
                except Exception as e:
                    st.error(f"An error occurred: {e}")
        else:
            st.warning("Please upload a DDL file first.")

# ==========================================
# PHASE 2 & 3: TALK TO YOUR DATA TAB
# ==========================================
elif page == "Talk to your data":
    st.title("Talk to Your Data 🗣️")
    st.write("Ask questions about the data saved in your PostgreSQL database!")

    try:
        inspector = inspect(engine)
        table_names = inspector.get_table_names()

        if not table_names:
            st.warning("No data found in the database. Go to the first tab and generate some data first!")
        else:
            selected_table = st.selectbox("Select a database table to analyze:", table_names)
            
            df_schema = pd.read_sql_table(selected_table, engine)
            st.write(f"Columns in **{selected_table}**: {', '.join(df_schema.columns)}")

            user_question = st.text_input(f"Ask a question about this data (e.g., 'Who lives in New York?'):")

            if st.button("Ask AI", key="ask_ai_btn"):
                if user_question:
                    with st.spinner("Generating and running SQL query..."):
                        
                        prompt = f"""
                        You are a PostgreSQL expert. Write a SQL query to answer the user's question.
                        Table Name: {selected_table}
                        Columns: {df_schema.columns.tolist()}
                        User Question: "{user_question}"
                        
                        CRITICAL RULE: You MUST wrap the table name and all column names in double quotes in your SQL query (e.g., SELECT "first_name" FROM "{selected_table}") because PostgreSQL is case-sensitive.
                        
                        Return a JSON object with a single key called "query" that contains the raw PostgreSQL code. Do not include any other text.
                        """
                        
                        response = client.models.generate_content(
                            model='gemini-2.5-flash',
                            contents=prompt,
                            config=types.GenerateContentConfig(
                                response_mime_type="application/json",
                            )
                        )
                        
                        # 1. Push to Langfuse
                        send_to_langfuse(user_question, prompt, response.text)

                        # 2. Extract and run SQL
                        sql_query = json.loads(response.text)["query"]
                        st.info(f"**The AI generated this query:** \n`{sql_query}`")
                        
                        try:
                            result_df = pd.read_sql(sql_query, engine)
                            st.success("Here are your results:")
                            st.dataframe(result_df)
                        except Exception as sql_error:
                            st.error(f"Failed to run the query: {sql_error}")
                else:
                    st.warning("Please type a question first.")
                    
    except Exception as e:
        st.error(f"Database error: {e}")