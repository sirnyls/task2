import pandas as pd
from transformers import RobertaTokenizer, RobertaForSequenceClassification, AdamW
import torch
from transformers import Trainer, TrainingArguments
import numpy as np
from datasets import Dataset, DatasetDict
from sklearn.metrics import accuracy_score, recall_score, precision_score, f1_score
from transformers import set_seed
from sklearn.metrics import mean_squared_error,mean_absolute_error,r2_score,classification_report
from datasets import load_metric
from sklearn.model_selection import train_test_split
from sklearn.utils import class_weight
from torch import nn
import transformers
import wandb

def compute_metrics_discrete(eval_pred):
    logits, labels = eval_pred
    predictions = np.argmax(logits, axis=-1)
    accuracy = accuracy_score(y_true=labels, y_pred=predictions)
    cr=classification_report(labels,predictions,output_dict=True)
    recall_w = recall_score(y_true=labels, y_pred=predictions,average='weighted')
    precision_w = precision_score(y_true=labels, y_pred=predictions,average='weighted')
    f1_micro = f1_score(y_true=labels, y_pred=predictions,average='micro')
    f1_weighted = f1_score(y_true=labels, y_pred=predictions,average='weighted')
    return {"accuracy": accuracy, "f1_0":cr['0']['f1-score'],"f1_1":cr['1']['f1-score'],
            "precision_1":cr['1']['precision'],"recall_1":cr['1']['recall'],
             "precision_w": precision_w, "recall_w": recall_w,
            "f1_micro": f1_micro,"f1_weighted": f1_weighted} 

def process_data(file_path,dataset,amr=True,outcome_variable='helpfulness'):
    """Process data for training RoBERTa model, formatting depends on the dataset"""
    df=pd.read_csv(file_path)
    if amr:
        if dataset in ['PAWS']:
            df=df.assign(text="Sentence 1: "+df.premise_+"\nAMR 1: "+df.amr_p+"\nSentence 2: "+df.hypothesis_+"\nAMR 2: "+df.amr_h)
        elif dataset in ['translation','logic','django','spider']:
            df=df.assign(text="Text: "+df.text+"\nAMR: "+df.amr)
        elif dataset in ['pubmed']:
            df=df.assign(text="Text: "+df.text+"\nInteraction: "+df.interaction+"\nAMR: "+df.amr)
    else:
        if dataset in ['PAWS']:
            df=df.assign(text="Sentence 1: "+df.amr_p+"\nSentence 2: "+df.hypothesis_)
        elif dataset in ['translation','logic','django','spider']:
            df=df.assign(text="Text: "+df.text)
        elif dataset in ['pubmed']:
            df=df.assign(text="Text: "+df.text+"\nInteraction: "+df.interaction)
    
    if outcome_variable=='helpfulness':
        df=df.assign(label=np.where(df.helpfulness<=0,0,1))
    elif outcome_variable=='did_llm_failed':
        df=df.assign(label=df.did_llm_failed)
    df=df.loc[:,['id','text','label']]
    df=df.loc[~df.text.isna()]
    return df

def split_sets(dataset,df):
    """Split data into train, dev and test sets, formatting depends on the dataset"""
    if dataset in ['translation']:
        df['set']=df.id.str[:10]
        train_set=df.loc[df['set']=='newstest13']
        dev_set, test_set = train_test_split(df.loc[df['set']=='newstest16'], test_size=0.5,random_state=42)
    elif dataset in ['PAWS','pubmed']:
        train_set, val_df = train_test_split(df, test_size=0.3,random_state=42)
        dev_set, test_set = train_test_split(val_df, test_size=0.5,random_state=42)
    elif dataset in ['logic','django','spider']:
        train_set=df.loc[df['id'].str.contains('train')]
        test_set=df.loc[df['id'].str.contains('test')]
        dev_set=df.loc[df['id'].str.contains('dev')]
    
    return train_set,dev_set,test_set


def tokenize(batch):
    return tokenizer(batch["text"], padding=True, truncation=True, max_length=512)

def model_init():
    transformers.set_seed(42)
    m = RobertaForSequenceClassification.from_pretrained(logs_path+"models/"+run_name, num_labels=2,device_map='auto')
    
    #m.roberta.apply(freeze_weights)

    ### new fine tune approach
    # Freeze base layers of RoBERTa
    for param in m.roberta.parameters():
        param.requires_grad = False

    # Fine-tune the classification head
    for name, param in m.classifier.named_parameters():
        param.requires_grad = True
    
    ## end of fine tune approach

    return m



class CustomTrainer(Trainer):
    def compute_loss(self, model, inputs, return_outputs=False):
        labels = inputs.get("labels")
        # forward pass
        outputs = model(**inputs)
        logits = outputs.get("logits")
        # compute custom loss (suppose one has 2 labels with different weights)
        loss_fct = nn.CrossEntropyLoss(weight=torch.tensor(class_weights, device=model.device,dtype=torch.float))
        loss = loss_fct(logits.view(-1, self.model.config.num_labels), labels.view(-1))
        return (loss, outputs) if return_outputs else loss

def freeze_weights(m):
    for name, param in m.named_parameters():
        param.requires_grad = False  

dataset='PAWS'
#datasets=['PAWS','translation','pubmed','logic','django','spider']
## True for balancing the observations in the loss function (currently not working)
compute_weights=False
current=-1
d_metric='f1_1'
amr_flag=True
decision_metric='eval_'+d_metric
outcome_variable='helpfulness'
## final results files
##https://drive.google.com/drive/folders/17pwdiiu7U1oyly8YwMtqCRdu3GBIWT3K
file_path='../../processed/predictions/final_results_paws.csv'
logs_path='../../processed/predictions/'
run_name=dataset+"_hyp_final_"+outcome_variable

df=process_data(file_path=file_path,dataset=dataset,amr=amr_flag,outcome_variable=outcome_variable)
train_set,dev_set,test_set=split_sets(dataset=dataset,df=df)

if compute_weights:
    class_weights=class_weight.compute_class_weight(class_weight='balanced',classes=train_set.label.unique(),y=train_set.label.values)
else:
    ## same weights but balance dataset
    class_weights=np.ones(df.label.unique().shape[0])

## prepare sets
set_seed(42)
torch.manual_seed(42)
tokenizer = RobertaTokenizer.from_pretrained('roberta-large')

train_dataset=Dataset.from_pandas(train_set)
val_dataset=Dataset.from_pandas(dev_set)
test_dataset=Dataset.from_pandas(test_set)
train_dataset = train_dataset.map(tokenize, batched=True, batch_size=len(train_dataset))
val_dataset = val_dataset.map(tokenize, batched=True, batch_size=len(val_dataset))
test_dataset = test_dataset.map(tokenize, batched=True, batch_size=len(test_dataset))
train_dataset.set_format("torch", columns=["input_ids", "attention_mask", "label"])
val_dataset.set_format("torch", columns=["input_ids", "attention_mask", "label"])
test_dataset.set_format("torch", columns=["input_ids", "attention_mask", "label"])

training_args = TrainingArguments(
    output_dir=logs_path+'results/'+run_name,
    report_to=None,
    evaluation_strategy='epoch',
    save_strategy='epoch',
    learning_rate=2e-5,
    per_device_train_batch_size=32,
    per_device_eval_batch_size=32,
    save_total_limit=1,
    num_train_epochs=15,
    weight_decay=0.01,
    warmup_steps=500,
    push_to_hub=False,
    logging_dir=logs_path+'logs/'+run_name,
    logging_steps=15,
    seed=42,
    load_best_model_at_end=True,
    metric_for_best_model=decision_metric,
    greater_is_better=True,
)

trainer = CustomTrainer(
    model_init=model_init,
    args=training_args,
    train_dataset=train_dataset,
    eval_dataset=val_dataset,
    compute_metrics=compute_metrics_discrete,
)

print("##### VALIDATION RESULTS#####")
res_val=trainer.evaluate()
print(res_val)
print("Decision metric ",'eval_',d_metric,": ",res_val['eval_'+d_metric])

res=trainer.predict(test_dataset)
print(res.metrics)
print("##### TEST RESULTS#####")
print("Variable: ",outcome_variable)
print("Dataset: ",dataset)
print("Decision metric ",'test_',d_metric,": ",res.metrics['test_'+d_metric])
