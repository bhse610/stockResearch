import yfinance as yf
import json
import gradio as gr
import os
from openai import OpenAI
from IPython.display import Markdown, display
import streamlit as st
import pandas as pd
import numpy as np

#input stock list
inputFile = "data/input/stockList.json"
outputFile ="data/output/analysisSummary.json"

#load data from json
def readDataFromJson(inFile):
    try:
        with open(inFile, 'r') as file:
            jsonInputData = json.load(file)
    # file not found error    
    except FileNotFoundError:
        print("Error: The file was not found.", inputFile, "inputFile")
    #json decoded error
    except json.JSONDecodeError:
        print("Error: Failed to decode JSON from the file.")
    return jsonInputData


#write data to json file
def writeDataToJson(outFile, outData):
    with open(outFile, 'w+') as file:
        for data in outData :
            json_str = json.dumps(data, indent=4)
            file.write(json_str)
            file.write(",\n")
    file.close()

#process Input data
def processInputData(inDatas):
    outData=[]
    #outData=json.loads(outData)
    for inData in inDatas['IND']:
        stockData = yf.Ticker(inData["symbol"]+".NS")
        outData.append({"Symbol": inData["symbol"],"PE": stockData.info['trailingPE']})
        for key, value in stockData.info.items():
            pass#print(f"{key}: {value}")
    return outData

def displayResult(outData):
    data = pd.DataFrame(outData)
    config = {
        "Symbol": "Stock Symbol",
        "PE": "Price to Earnings Ratio"
    }
    st.dataframe(data, column_config=config, use_container_width=True)

def aiAgent():
    deepseek_api_key = os.getenv('DEEPSEEK_API_KEY')
    openai = OpenAI(api_key=deepseek_api_key, base_url="https://api.deepseek.com")
    messages = [{"role": "user", "content": "How is TCS performing in the market?"}]
    response = openai.chat.completions.create(
    model="deepseek-chat",
    messages=messages
    )
    print(response.choices[0].message.content)
    return response.choices[0].message.content
# main for function call.
if __name__ == "__main__":
    jsonData = readDataFromJson(inputFile)
    outputData = processInputData(jsonData)
    writeDataToJson(outputFile, outputData)
    print(Markdown(aiAgent()))    #displayResult()
    displayResult(outputData)
