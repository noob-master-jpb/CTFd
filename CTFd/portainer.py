import configparser
import requests
import urllib3
import json
import os

from dotenv import load_dotenv

load_dotenv()

#ssl warning supression -- DO NOT REMOVE
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning) 
requests.packages.urllib3.disable_warnings()



#modify the following function according to your config
def api_key():
    return str(os.getenv("PORTAINER_API_KEY"))
    config = configparser.ConfigParser()
    # try:
    #     config.read(f"{os.getenv('CONFIG_INI_URL')}")
    #     print(config['portainer']['api_key'])
    #     return config['portainer']['api_key']
    # except:
    #     raise Exception("api_key path not found")
    
def payload(port=None,image=None):

    if not port:
        print("\nNo port Provided\n")
        return 

    if not image:
        print("\nNo image Provided\n")
        return 

    

    try:
        file = open(f"{os.getenv('CHAL_PAYLOAD_FILE_URL')}","r")
        payload = json.load(file)
        try:
            #change these lines accordingly
            payload["Image"] = str(image)
            payload["HostConfig"]["PortBindings"]["80/tcp"][0]["HostPort"] = str(port) 
            
            return payload
        except KeyError:
            print("\nBad templete for payload\n")
            return
    except FileNotFoundError:
        print("\npayload file not found\n")
        return



def imageid(challenge_id):
    file = open(f"{os.getenv('CHAL_MAP_FILE_URL')}","r")
    map = json.load(file)
    try:
        
        return map[challenge_id]
        
    except KeyError:
        print(f"image_id for challenge_id:{challenge_id} does not exists")
        return

def endpoint():
    return str(os.getenv('PORTAINER_ENDPOINT')) #configure this accordingly

def ip():
    return str(os.getenv('PORTAINER_VM_IP'))


#not in use but can be useful if needed

def list_endpoints(base_ip= "portainer",base_port= "9443",key=None):
    
    if not key:
        raise Exception("Please an api key")

    headers = {'Authorization':f"Bearer {key}", 
               'Content-Type': 'application/json'}
    
    location = f"api/endpoints"
    try:
        url = f"https://{base_ip}:{base_port}/{location}"

        response = requests.get(url, headers=headers, verify=False)
        return response
    except Exception as e:
        print('There was a problem with the request:', e)
        return 
    

def list_container(base_ip= "portainer",base_port= "9443",endpoint=None,key=None):
    if not key:
        raise Exception("Please an api key")

    
    if not endpoint:
        raise Exception("No endpoint Provided")
    
    headers = {'Authorization':f"Bearer {key}", 
               'Content-Type': 'application/json'}

    location = f"api/endpoints/{endpoint}/docker/containers/json"
    try:
        url = f"https://{base_ip}:{base_port}/{location}"

        response = requests.get(url, headers=headers, verify=False)
        return response
    
    except Exception as e:
        print('There was a problem with the request:', e)
        return 
    



#functions are in use do not modify them until needed  

def create_continers(base_ip= "portainer",base_port= "9443",endpoint=None,key=None,name= None,payload = None):

    if not key:
        raise Exception("Please an api key")
    
    if not endpoint:
        raise Exception("No endpoint Provided")

    if not name:
        raise Exception("No container name provided")

    if not payload:
        raise Exception("No payload found")

    try:
        container_name = str(name)
    except:
        raise Exception("Improper container name")
    

    headers = {'Authorization':f"Bearer {key}", 
               'Content-Type': 'application/json'}
    
    

    location = f"api/endpoints/{endpoint}/docker/containers/create?name={container_name}"
    try:
        url = f"https://{base_ip}:{base_port}/{location}"
        response = requests.post(url=url,headers=headers,json=payload,verify=False)
        return response
    except requests.exceptions.HTTPError as e:
        print(f"HTTP error occurred: {e}")
        raise
    except requests.exceptions.RequestException as e:
        print(f"Request error occurred: {e}")
        raise
    





def start_container(base_ip = "portainer",base_port= "9443",endpoint_id= None,key = None,container_id= None):

    if not api_key:
        raise ValueError("Please provide an API key.")
    if endpoint_id is None:
        raise ValueError("Please provide an endpoint ID.")
    if not container_id:
        raise ValueError("Please provide a container ID.")
    
    url = f"https://{base_ip}:{base_port}/api/endpoints/{endpoint_id}/docker/containers/{container_id}/start"
    
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }
   
    try:
        response = requests.post(url=url, headers=headers, verify=False)
        response.raise_for_status()
        return response
    except requests.exceptions.HTTPError as e:
        print(f"HTTP error occurred: {e}")
        raise
    except requests.exceptions.RequestException as e:
        print(f"Request error occurred: {e}")
        raise




def delete_containers(base_ip= "portainer",base_port= "9443",endpoint=None,key=None,id= None):
    if not key:
        raise Exception("Please an api key")
    
    if not endpoint:
        raise Exception("No endpoint Provided")

    if not id:
        raise Exception("No container name provided")

    location = f"api/endpoints/{endpoint}/docker/containers/{id}?force=true"

    
    headers = {'Authorization':f"Bearer {key}", 
               'Content-Type': 'application/json'}

    try:
        url = f"https://{base_ip}:{base_port}/{location}"
        response = requests.delete(url=url,headers=headers,verify=False)

        return response

    except requests.exceptions.HTTPError as e:
        print(f"HTTP error occurred: {e}")
        raise
    except requests.exceptions.RequestException as e:
        print(f"Request error occurred: {e}")
        raise