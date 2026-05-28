import numpy as np
import matplotlib.pyplot as plt
import matplotlib.widgets as widgets
import sys
# sys.path.insert(1,"C:\\windows\\python") #path has to be correct here for the y2daq location
import y2daq

# original code
def move_buggy():
    while start_buggy == 'True':
        # choose required output depending on status of radio buttons
        if radioHandle.value_selected == 'forward':
            y = np.array([51,102,204,153],dtype=np.uint8)
        elif radioHandle.value_selected == 'backward':
            y = np.array([153,204,102,51],dtype=np.uint8)
#        elif radioHandle.value_selected == 'left':
#            y = np.array([153,204,102,51],dtype=np.uint8)        
#        elif radioHandle.value_selected == 'right':
#            y = np.array([153,204,102,51],dtype=np.uint8)   
            
        for i in range(4):
            d.write(np.unpackbits(y[i])) # digital output
            plt.pause(sliderHandle.val) # wait for motor to move slider controls speed

# pauses every other step (15 degrees) instead of every step (7.5 degrees) to allow for manual image capture
'''def move_buggy():
    while start_buggy == 'True':
        if radioHandle.value_selected == 'forward':
            y = np.array([51,102,204,153], dtype=np.uint8)
        elif radioHandle.value_selected == 'backward':
            y = np.array([153,204,102,51], dtype=np.uint8)

        # 15 degrees = 2 full cycles of 7.5°
        cycles_per_image = 2

        for c in range(cycles_per_image):
            for i in range(4):
                d.write(np.unpackbits(y[i]))
                plt.pause(sliderHandle.val)
'''

def startCallback(event):
    global start_buggy
    start_buggy = 'True'
    move_buggy()
def stopCallback(event):
    global start_buggy
    start_buggy = 'False'
def closeCallback(event):
    d.clear()
    d.__end_()
    plt.close('all') #close all open figure windows

# Create digital output object
d=y2daq.digital()
# Set up the user interface
fig=plt.figure(figsize=(4,4))
# Radio buttons control the direction of the buggy
rax=plt.axes([0.3,0.4,0.4,0.3])
radioHandle=widgets.RadioButtons(rax,('forward','backward'),active=0)
# Button to start the buggy
startax=plt.axes([0.2,0.28,0.25,0.1])
startHandle=widgets.Button(startax,'Start')
startHandle.on_clicked(startCallback)
# Button to stop the buggy
stopax=plt.axes([0.55,0.28,0.25,0.1])
stopHandle=widgets.Button(stopax,'Stop')
stopHandle.on_clicked(stopCallback)
# Button to close the GUI
bax=plt.axes([0.4,0.75,0.2,0.1])
closeHandle=widgets.Button(bax,'Close')
closeHandle.on_clicked(closeCallback)

#bax2=plt.axes([0.75,0.5,0.2,0.1])
#closeHandle=widgets.Button(bax2,'left')
#closeHandle.on_clicked(closeCallback)
#
#bax3=plt.axes([0.05,0.5,0.2,0.1])
#closeHandle=widgets.Button(bax3,'right')
#closeHandle.on_clicked(closeCallback)

sax=plt.axes([0.25,0.05,0.5,0.03])
sliderHandle=widgets.Slider(sax,'Speed',0.02,5,valinit=0.02)