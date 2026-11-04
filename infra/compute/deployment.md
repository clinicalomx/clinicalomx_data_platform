# Infrastructure Deployment
Deploy order and procedure for on-demand dev boxes. 

Run everything from `infra/compute`
```{bash}
cd clinicalomx_data_platform/infra/compute
```


## 0. Requirements
```
python-openstackclient
opentofu
```

## 1. Source Nectar Cloud environment variables

Source Nectar environmental variables.
See https://support.ehelp.edu.au/support/solutions/articles/6000078065-getting-authenticated-for-api-command-line-use.
```{bash}
source <your Nectar RC>
```
This exports the following env vars automatically:
```
OS_AUTH_URL
OS_PROJECT_DOMAIN_NAME
OS_USER_DOMAIN_NAME
OS_USERNAME
OS_AUTH_TYPE
OS_PROJECT_NAME
OS_PASSWORD
```
These are used for the next steps.

## 2. Create/source variables and instances for object storage
Openstack uses the Amazon S3 protocol to connect to Nectar object
storage.

```{bash}
export AWS_REGION=us-east-1 # Ceph RGW zonegroup literal, must be this
export AWS_ENDPOINT_URL_S3=https://objects.example.edu/ # the object store's S3 endpoint
```

Create EC2 credentials if not already done:
```{bash}
openstack ec2 credentials create
```

If created already, then retrieve with:
```{bash}
openstack ec2 credentials list
```

Export the respective keys into:
```{bash}
export AWS_ACCESS_KEY_ID=... 
export AWS_SECRET_ACCESS_KEY=... 
```

DO ONCE ONLY - create the object storage to store the opentofu variables
in encrypted object storage:
```{bash}
openstack container create <state-bucket>  # then name it in TF_CLI_ARGS_init in .envrc
```

## 3. Source OpenTofu environment variables
The following tf vars need to be exported during runtime:
```{bash}
export TF_VAR_state_encryption_passphrase='...' # set to any or autogenerate this
export TF_CLI_ARGS_init=-backend-config=bucket=<state-bucket> # the container from step 2
export TF_VAR_ghcr_username='...' # your github user
export TF_VAR_ghcr_packages_read_pat='...' # classic PAT, read:packages only
export TF_VAR_github_contents_read_pat='...' # fine-grained PAT, Contents: read
```

## 4. Create each node's dedicated volume once (it becomes /home on the box):
```{bash}
openstack volume create --size 200 compute-tau-data
```

## 5. Set hub_instance_name in compute.tfvars to the control plane
See `infra/compute/compute.tfvars.example`.

## 6. Initialise, plan, review (all resources should be "to add"), and apply.
```{bash}
tofu init -backend-config="key=prod/compute/terraform.tfstate"
tofu plan -var-file=compute.tfvars -out=compute.plan
tofu apply compute.plan
```

## 7. Read the login details and hand each user their ssh line.
```{bash}
tofu output dev_boxes
```

## 8. Tear down
Removing its compute_nodes entry from compute.tfavrs, then run step 3 again. 

The VM and its floating IP are released.