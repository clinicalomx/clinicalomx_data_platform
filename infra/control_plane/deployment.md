# Infrastructure Deployment
Deploy order and procedure for the control_plane / main server. 

Run everything from `infra/control_plane`
```{bash}
cd clinicalomx_data_platform/infra/control_plane
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

## 4. Initialise tofu
```{bash}      
tofu init -backend-config="key=prod/terraform.tfstate" -reconfigure
```

## 5. Static checks.
```{bash}
tofu fmt -check
tofu validate
```

## 6. Create tofu plan
Checks the plan of actions this deployment will take.
```{bash}
tofu plan -var-file=prod.tfvars -out=prod.plan
```

## 7. Confirm the plan is small and non-destructive.
If deployment is live, only changes should be:
  - a new compute security group and its rules:
    ```
      + openstack_networking_secgroup_v2.compute
      + openstack_networking_secgroup_rule_v2.compute["postgres"|"mlflow"|"prefect"]
    ```  
  - an in-place update of the hub port adding that security group:
    ```
      + openstack_networking_port_v2.hub   (security_group_ids)
    ```
  - one SSH-rule replace, recreated, admin SSH access unchanged:
    ```
      - openstack_networking_secgroup_rule_v2.ssh
      + openstack_networking_secgroup_rule_v2.ssh["VMIP/32"]
    ```
Be careful if the plan shows any of the following:
```
  - openstack_compute_instance_v2.hub replaced or destroyed (would rebuild the VM);
  - any openstack_compute_volume_attach_v2.* destroyed (would detach the DB /
        artifacts / docker volumes);
  - openstack_networking_floatingip_associate_v2.hub destroyed, or any
        openstack_keymanager_secret_v1.* replaced;
  - openstack_networking_port_v2.hub replaced
  - everything shown as "to add", which means tofu is not seeing existing state
        (wrong key, wrong passphrase, or empty state) - fix init before proceeding.
```

If deploying for the first time, expect every to be newly added.


## 8. Apply the reviewed artifact (not a fresh plan).
```{bash}
tofu apply prod.plan
```

## 9. Verify the hub is untouched and reachable.
NOTE: `USER` will depend on the default user set by the VM flavor. 

See the cloud provider's guide to connecting over SSH.
```
ssh USER@$(tofu output -raw vm_ip) # 
docker ps # postgres + mlflow + prefect still up
systemctl status clinicalomxdb
```

# (Optional) Deploy the compute dev boxes
Only after the control plane is applied (its compute security group must exist first,
and the box joins the hub's private subnet). 


Run everything from `infra/compute`
```{bash}
cd clinicalomx_data_platform/infra/compute
```

Run from infra/compute. This root needs
OS_*, AWS_*, and TF_VAR_state_encryption_passphrase. (Run steps 1-3)

## 1. Create each node's dedicated volume once (it becomes /home on the box):
```{bash}
openstack volume create --size 200 compute-tau-data
```

## 2. Set hub_instance_name in compute.tfvars to the control plane
See `infra/compute/compute.tfvars.example`.

## 3. Initialise, plan, review (all resources should be "to add"), and apply.
```{bash}
tofu init -backend-config="key=prod/compute/terraform.tfstate"
tofu plan -var-file=compute.tfvars -out=compute.plan
tofu apply compute.plan
```

## 4. Read the login details and hand each user their ssh line.
```{bash}
tofu output dev_boxes
```

## 5. Tear down
Removing its compute_nodes entry from compute.tfavrs, then run step 3 again. 

The VM and its floating IP are released.