import yfinance as yf
import json
import gradio as gr

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

#write json
def write_json(new_data, filename='data.json'):
    with open(filename, 'r+') as file:
        # Load existing data into a dictionary
        file_data = json.load(file)
        
        # Append new data to the 'emp_details' list
        file_data["IND"].append(new_data)
        
        # Move the cursor to the beginning of the file
        file.seek(0)
        
        # Write the updated data back to the file
        json.dump(file_data, file, indent=4)
#write data to json file
def writeDataToJson(outFile, outData):
    for data in outData :
        write_json(data,outFile)

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


# main for function call.
if __name__ == "__main__":
    jsonData = readDataFromJson(inputFile)
    outputData = processInputData(jsonData)
    writeDataToJson(outputFile, outputData)