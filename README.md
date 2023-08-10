# ECK, BBQ and Pi

## Intro

The purpose of this is a *relatively* straightforward demo architecture that also has a somewhat useful real world function as well.  It has a fairly simple data flow conceptually, and can be be built upon to show more sophisticated use cases. 

## Base Diagram

```mermaid
flowchart LR
  Inkbird("Inkbird BBQ Thermometer") --> |Bluetooth|RaspberryPi4

  subgraph RaspberryPi4[Raspberry Pi 4]
    Agent[Agent]
    Python[Python]
  end
  
RaspberryPi4 --> ElasticStack

  subgraph ElasticStack[Elastic Stack]
    ES1[Elasticsearch-1]
    ES2[Elasticsearch-2]
    ES3[Elasticsearch-3]
    Kibana[Kibana]
    Fleet[Fleet Server]
    Agents[Agents]
  end
```

## Basic Architecture Explanation



### What's all this then?

Elasticsearch is a distributed, JSON based search and analytics engine.  It has many core use cases including but not limited to text search, observability, and security.  We will use it to send JSON documents into a cluster of nodes and by default it indexes every field making it easy to compare and analyze data.  

ECK is an Kubernetes operator maintained by Elastic itself.  An operator is a Red Hat term that essentially allows us to take a Kubernetes environment, add our own custom object types, and our own controller that will monitor these objects and correct them to the desired state.  This is a huge advantage versus maintaining your own automation as I all too well know. It is the single best way to self manage Elastic stack infrastructure in my humble opinion.  Again, these are my opinions and not necssarily those of Elastic itself.

I used components of the Elastic Stack managed by ECK to make an Elastic cluster that handles the bluetooth temperature data.  A Raspberry Pi runs Python code near the Bluetooth BBQ temperature sensor that captures the data and relays it back to the Elastic cluster. 

### ECK Cluster

For the Kubernetes initiated, you need to have the ECK operator configured ([Quickstart](https://www.elastic.co/guide/en/cloud-on-k8s/current/k8s-deploy-eck.html)) and from there you can simply apply the three manifests contained here. These are reasonable defaults but feel free to tweak them.  The manifests themselves come from the [Kittyhawk](https://github.com/jdel12/eck-kittyhawk/) repo  which I also encourage you to check out.  For the demo architecture, you will get three nodes of Elasticsearch, one Kibana instance (UI), a Fleet Server for managing Elastic Agents, and then the Agents themselves as a Daemonset.  If you need to make changes or tweaks, please do. 

### Raspberry Pi

In this case I used a Raspberry Pi 4 but any Raspberry Pi 3 or 4 (arm64) should work. I would imagine you maybe be able to go further back but I can't promise the libraries and dependencies will work the same. I used Ubuntu 22.X LTS. 

To prepare the Pi for the Python code, you can run these commands. Keep in mind, I used sudo here for much of it but I would always encourage you to be careful which user is performing tasks like pip. For the more python inclined, absolutely consider venv and/or a non-root user. I included curl to make sure you can easily install the Agent.

```
sudo apt-get install libglib2.0-dev pkg-config python3-pip curl
sudo pip install bluepy
```

To setup an Elastic Agent on the device, see below commands which you can also find examples up while looking at Fleet in Kibana. Please note the url should be for the fleet server itself and typically uses https.  You can add an `--insecure` flag but needless to say, not advisable unless you are just hacking around.  The enrollment token can come from the Fleet UI page in Kibana (From the left hand menu, scroll to the bottom and look for 'Fleet'). 

```
curl -L -O https://artifacts.elastic.co/downloads/beats/elastic-agent/elastic-agent-8.8.1-linux-arm64.tar.gz
tar xzvf elastic-agent-8.8.1-linux-arm64.tar.gz
cd elastic-agent-8.8.1-linux-arm64/
sudo ./elastic-agent install --url=<fleet-server-url> --enrollment-token=<token-from-fleet>
```

Finally, get the python script from this repo onto the device however you'd like (git clone, copy paste, scp, a usb stick, whatever works for you) and run the command. Again, consider the implications of which user both from a systems point of view and also the elasticsearch applicaiton level for authentication. You will need to populate several varaibles in the Python script (look at approximately lines 42-76) unique to wherever you run things - namely basic auth for elastic (user:password), the cluster URL, the index name you want.  Please also know that you likely will need to specify the date type for this timestamp so it isnt just seen as an integer. 

In the Kibana menu, look for Management>Dev Tools and run this command to setup the index date field before sending data. 

```
PUT <indexname>/
{
    "mappings": {
        "properties": {
            "date": {
                "type": "date",
                "format": "strict_date_optional_time||epoch_second"
            }
        }
    }
}
```

To run the python script, you can use a command like below. 

```
sudo python3 <bbq-python-script>
```

If you are using self signed certs, you can (this is the opposite of best practice) use `verify=False` in the POST requests as a flag. 

### Inkbird IBT-4XS Bluetooth Wireless Grill BBQ Thermometer

Nothing. You should just be able to plug and play with this.  Again, in principle any bluetooth enabled sensor using the ibbq libraries should be compatable. 

See below for a sample document:

`{'bbq_temp': 225, 'bbq_probe': 1, 'date': 1691290406}`

### Changes and Additions Made for the Demo Specifically

For this demo, I applied the above Kubernetes yamls but added external load balancers to expose things publicly for my Pi to relay the info back.  For the Agent going on the Pi, I also added the [Integrations](https://www.elastic.co/integrations/data-integrations?solution=all-solutions) for Elastic Defend, OSQuery Manager (on top of System) and created a separate policy for the Pi.  You technically do not need the Agent for the python to work to be very clear but all of this is freely available and adds some sophistication as well as just great fundamental security and observability. See below for some enhancements that could be added with this as well. 


----------

## Future Enhancements I'd Like to Add

- APM Instrumentation of the Python Code (Python Client, APM Fleet Integration)
- Include video or photos from a camera, likely triggered by alerts like temperature spikes/drops or ?
- Eland Code (Jupyter Notebooks) for Pandas/Python exploration of the data captured
- Use the `_bulk` API to pump data every few seconds and cut down on volumous network calls
- Add Elastic's Universal Profiling features to the Pi (and possibly ECK cluster as well)
- Build in templating for date fields or more to come
- Add environmental data from your location (outdoor temperatures, humidity, etc.)
- Add Dockerization, K3S manifests for everything
- Add OOTB Fleet packages for OSQuery, APM, and Defend to kibana manifest (vs using the UI which is perfectly valid as well)
- Add some kind of code functionality to restart connections when/if the script pauses during long cooks. Inkbird appears to want to maintain the original connection. Evaluating options. 


## Acknowledgements

The Python here is heavily borrowed and inspired by the work from the repository at [https://github.com/8none1/pybq](https://github.com/8none1/pybq) by [8none1](https://github.com/8none1).  Many of the code comments are straight from the original but I found them helpful so I left them in.  I have cleaned up some things and edited it to focus on Elasticsearch etc. but want to give credit where due. Great work by 8none1 and thank you!